"""All server-statistics commands live under one /stats group: roles, activity, playing,
tags and badges. Previously these were six separate top-level commands (/roletop, /activity,
/cbc, /cbu, /playing, /guildtags) spread across four files — grouped here so they're
discoverable in one place and share a consistent embed style."""

import discord
from discord import app_commands
from discord.ext import commands
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

BADGE_ATTRS = {
    "staff": "Discord Staff",
    "partner": "Discord Partner",
    "hypesquad": "HypeSquad Events",
    "bug_hunter": "Bug Hunter L1",
    "bug_hunter_level_2": "Bug Hunter L2",
    "hypesquad_bravery": "House Bravery",
    "hypesquad_brilliance": "House Brilliance",
    "hypesquad_balance": "House Balance",
    "early_supporter": "Early Supporter",
    "team_user": "Team User",
    "verified_bot": "Verified Bot",
    "verified_bot_developer": "Early Verified Bot Developer",
    "discord_certified_moderator": "Moderator Programs Alumni",
    "bot_http_interactions": "HTTP Interactions Bot",
    "active_developer": "Active Developer",
}

COLOR_ROLES = discord.Color.blue()
COLOR_ACTIVITY = discord.Color.green()
COLOR_PLAYING = discord.Color.teal()
COLOR_TAGS = discord.Color.blurple()
COLOR_BADGES = discord.Color.gold()


async def _badge_autocomplete(interaction: discord.Interaction, current: str):
    choices = [app_commands.Choice(name="All Badges", value="all")]
    for key, name in BADGE_ATTRS.items():
        choices.append(app_commands.Choice(name=name, value=key))
    if current:
        choices = [c for c in choices if current.lower() in c.name.lower()]
    return choices[:25]


def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
    """Join lines into field-sized (<=1024 char) blocks."""
    chunks, current = [], ""
    for line in lines:
        line = line[:limit]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks or [""]


class Stats(commands.GroupCog, name="stats", description="Server statistics: roles, activity, badges, tags and more"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _base_embed(self, title: str, color: discord.Color, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    def _footer(self, embed: discord.Embed, interaction: discord.Interaction):
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )

    # ── /stats roles ──────────────────────────────────────────────
    @app_commands.command(name="roles", description="Show the most common roles in this server")
    @app_commands.describe(limit="How many roles to show (default 10, max 25)")
    async def roles(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        await interaction.response.defer()

        role_counts = Counter()
        for member in guild.members:
            for role in member.roles:
                if role.name != "@everyone":
                    role_counts[role] += 1

        if not role_counts:
            await interaction.followup.send("No roles found.")
            return

        total_members = len(guild.members)
        top_roles = role_counts.most_common(limit)
        lines = [
            f"`{i}.` {role.mention} — **{count}** members ({count / total_members * 100:.1f}%)"
            for i, (role, count) in enumerate(top_roles, 1)
        ]

        embed = self._base_embed(f"🏆 Top Roles", COLOR_ROLES, guild)
        embed.description = "\n".join(lines)
        embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
        embed.add_field(name="Total members", value=f"{total_members:,}", inline=True)
        embed.add_field(name="Roles shown", value=f"{len(top_roles)} of {len(role_counts)}", inline=True)
        self._footer(embed, interaction)
        await interaction.followup.send(embed=embed)

    # ── /stats activity ──────────────────────────────────────────────
    @app_commands.command(name="activity", description="Show message activity for a channel")
    @app_commands.describe(
        channel="Channel to analyze (defaults to this channel)",
        hours="How many hours back to look (default 24, max 168 = 1 week)",
    )
    async def activity(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
        hours: app_commands.Range[int, 1, 168] = 24,
    ):
        if not interaction.guild:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        target_channel = channel or interaction.channel
        # This command is open to everyone, so it must not become a way to read activity in
        # channels the caller can't otherwise see.
        if not target_channel.permissions_for(interaction.user).read_message_history:
            await interaction.response.send_message(
                f"❌ You don't have access to {target_channel.mention}.", ephemeral=True)
            return

        await interaction.response.defer()
        time_threshold = datetime.now(timezone.utc) - timedelta(hours=hours)

        try:
            messages = [m async for m in target_channel.history(limit=None, after=time_threshold)]

            if not messages:
                await interaction.followup.send(
                    f"No messages found in {target_channel.mention} in the last {hours} hour(s)."
                )
                return

            total_messages = len(messages)
            unique_users = len(set(m.author.id for m in messages))
            user_counts = Counter(m.author for m in messages)
            top_chatters = user_counts.most_common(5)
            hour_counts = Counter(m.created_at.hour for m in messages)

            embed = self._base_embed(f"📊 Activity — #{target_channel.name}", COLOR_ACTIVITY, interaction.guild)
            embed.add_field(name="Messages", value=f"{total_messages:,}", inline=True)
            embed.add_field(name="Unique users", value=f"{unique_users:,}", inline=True)
            embed.add_field(name="Avg / hour", value=f"{total_messages / hours:.1f}", inline=True)

            chatter_lines = [
                f"`{i}.` {user.mention} — {count} ({count / total_messages * 100:.1f}%)"
                for i, (user, count) in enumerate(top_chatters, 1)
            ]
            embed.add_field(
                name="💬 Top chatters",
                value="\n".join(chatter_lines) if chatter_lines else "None",
                inline=False,
            )

            if hour_counts:
                peak_hour, peak_count = hour_counts.most_common(1)[0]
                embed.add_field(
                    name="⏰ Peak hour",
                    value=f"{peak_hour:02d}:00 UTC ({peak_count} messages)",
                    inline=True,
                )
            embed.add_field(name="Window", value=f"Last {hours}h", inline=True)
            self._footer(embed, interaction)
            await interaction.followup.send(embed=embed)

        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ I don't have permission to read message history in {target_channel.mention}."
            )
        except Exception as e:
            await interaction.followup.send(f"❌ An error occurred: {e}")

    # ── /stats playing ──────────────────────────────────────────────
    @app_commands.command(name="playing", description="Show what games people are playing right now")
    @app_commands.describe(
        online_only="Only check online members? Much faster on large servers (default: on)",
        show_examples="Show a few player names per game? (default: on)",
    )
    async def playing(
        self,
        interaction: discord.Interaction,
        online_only: bool = True,
        show_examples: bool = True,
    ):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        await interaction.response.defer()

        if online_only:
            members = [m for m in guild.members if m.status != discord.Status.offline]
            subtitle = "Online members only"
        else:
            members = guild.members
            subtitle = "All members"

        game_players = defaultdict(list)
        for member in members:
            act = member.activity
            if isinstance(act, discord.Game) or (
                isinstance(act, discord.Activity) and act.type == discord.ActivityType.playing
            ):
                name = act.name.strip()
                if name:
                    game_players[name].append(member.display_name)

        embed = self._base_embed("🎮 Currently Playing", COLOR_PLAYING, guild)

        if not game_players:
            embed.description = "No one is playing a detectable game right now."
            embed.set_footer(text=f"Mode: {subtitle}")
            await interaction.followup.send(embed=embed)
            return

        sorted_games = sorted(game_players.items(), key=lambda x: len(x[1]), reverse=True)
        embed.description = f"**{len(game_players)}** different games • {len(members):,} members scanned"

        lines = []
        for game, players in sorted_games[:12]:
            line = f"**{game}** × {len(players)}"
            if show_examples and players:
                examples = ", ".join(players[:3])
                if len(players) > 3:
                    examples += f" +{len(players) - 3} more"
                line += f" — {examples}"
            lines.append(line)

        for i, chunk in enumerate(_chunk_lines(lines)):
            label = "Top games" if i == 0 else "Top games (cont.)"
            embed.add_field(name=label, value=chunk, inline=False)

        embed.set_footer(text=f"Mode: {subtitle} • Showing top {min(12, len(game_players))}")
        await interaction.followup.send(embed=embed)

    # ── /stats tags ──────────────────────────────────────────────
    @app_commands.command(name="tags", description="Show primary guild tags used by members")
    @app_commands.describe(online_only="Only check online members? Faster on large servers (default: on)")
    async def tags(self, interaction: discord.Interaction, online_only: bool = True):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        await interaction.response.defer()

        if online_only:
            members = [m for m in guild.members if m.status != discord.Status.offline]
            subtitle = "Online members only"
        else:
            members = guild.members
            subtitle = "All members"

        tag_counts = Counter()
        total_tagged = 0
        for member in members:
            primary = member.primary_guild
            if primary is None:
                try:
                    user = await self.bot.fetch_user(member.id)
                    primary = user.primary_guild
                except Exception:
                    continue
            if primary and primary.tag:
                tag_counts[primary.tag] += 1
                total_tagged += 1

        embed = self._base_embed("🏷️ Guild Tags", COLOR_TAGS, guild)
        embed.description = f"**{total_tagged}** tagged • {len(members):,} / {guild.member_count:,} members scanned"

        if tag_counts:
            top = tag_counts.most_common(12)
            embed.add_field(
                name=f"Top tags ({len(tag_counts)} unique)",
                value="\n".join(f"`{tag}` × {count}" for tag, count in top),
                inline=False,
            )
        else:
            embed.add_field(name="Top tags", value="No tags found among scanned members.", inline=False)

        embed.set_footer(text=f"Mode: {subtitle}")
        await interaction.followup.send(embed=embed)

    # ── /stats badges ──────────────────────────────────────────────
    @app_commands.command(name="badges", description="Count or list members with a Discord profile badge")
    @app_commands.describe(
        badge="Badge to check, or leave as 'All Badges' for a full breakdown",
        show_members="List member names instead of a count (requires picking a specific badge)",
    )
    @app_commands.autocomplete(badge=_badge_autocomplete)
    async def badges(self, interaction: discord.Interaction, badge: str = "all", show_members: bool = False):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        if badge != "all" and badge not in BADGE_ATTRS:
            await interaction.response.send_message(
                "❌ Unknown badge — pick one from the autocomplete list.", ephemeral=True)
            return
        if show_members and badge == "all":
            await interaction.response.send_message(
                "❌ Pick a specific badge to list its members — \"All Badges\" only supports counts.",
                ephemeral=True)
            return
        await interaction.response.defer()

        def has_badge(member: discord.Member, key: str) -> bool:
            return bool(member.public_flags and getattr(member.public_flags, key, False))

        members = guild.members

        if badge == "all":
            counts = {b: sum(has_badge(m, b) for m in members) for b in BADGE_ATTRS}
            counts = {b: c for b, c in counts.items() if c > 0}
            embed = self._base_embed("🏅 Badge Counts", COLOR_BADGES, guild)
            if counts:
                ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
                embed.description = "\n".join(
                    f"**{BADGE_ATTRS[b]}** — {c:,}" for b, c in ranked
                )
            else:
                embed.description = "No detectable badges among this server's members."
            self._footer(embed, interaction)
            await interaction.followup.send(embed=embed)
            return

        matches = [m for m in members if has_badge(m, badge)]
        label = BADGE_ATTRS[badge]

        if not show_members:
            embed = self._base_embed(f"🏅 {label}", COLOR_BADGES, guild)
            pct = (len(matches) / len(members) * 100) if members else 0
            embed.description = f"**{len(matches):,}** member(s) have this badge ({pct:.1f}% of the server)"
            self._footer(embed, interaction)
            await interaction.followup.send(embed=embed)
            return

        embed = self._base_embed(f"🏅 {label} — Members", COLOR_BADGES, guild)
        if not matches:
            embed.description = "No members have this badge."
        else:
            shown = matches[:100]
            lines = [m.mention for m in shown]
            for i, chunk in enumerate(_chunk_lines(lines)):
                label_field = "Members" if i == 0 else "Members (cont.)"
                embed.add_field(name=label_field, value=chunk, inline=False)
            if len(matches) > 100:
                embed.description = f"Showing 100 of **{len(matches)}** members."
            else:
                embed.description = f"**{len(matches)}** member(s)"
        self._footer(embed, interaction)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
