"""Discovery Helper — the new-member retention loop.

The cycle, spread across three places:

1. eventcog.on_member_join gives every joining member a role named after that day's date, so
   everyone who joins on the same day shares one "cohort" role.
2. main.mention_players (driven by the midday loop in main.py) finds the cohort that joined
   8 days ago, pings its role once in the discovery channel, and deletes the ping 2s later —
   a notification without the clutter.
3. On day 9 the same routine deletes the role and its record, so cohorts don't pile up.

The survey message this points people at is posted by /setchannel; button clicks are
acknowledged in eventcog.on_interaction.
"""

import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands

import Database

PING_AFTER_DAYS = 8
CLEANUP_AFTER_DAYS = 9
COLOR_INFO = 0x5865F2
COLOR_GOOD = 0x2ECC71
COLOR_WARN = 0xE67E22


class DiscoveryHelper(commands.Cog, name="Discovery Helper"):
    """Keeps new members engaged by nudging each join-day cohort about a week in."""

    def __init__(self, client: commands.Bot):
        self.Client = client
        self.bot = client

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.Client.MongoClient)

    # ── /setchannel ──────────────────────────────────────────────────
    @app_commands.command(
        name="setchannel", description="Post the experience survey here and use it for nudges"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_discovery_channel(self, interaction: discord.Interaction):
        servers = self._db["servers"]

        view = discord.ui.View()
        for i in range(10):
            view.add_item(discord.ui.Button(label=f"{i+1}", style=discord.ButtonStyle.blurple))

        message = await interaction.channel.send(
            content="How has your experience been with the server?", view=view
        )

        # $set rather than a whole-document replace, so other per-guild settings on this doc
        # (media log, mod log) survive.
        await self._run(
            servers.update_one,
            {"guild_id": interaction.guild.id},
            {"$set": {
                "discovery_channel": interaction.channel.id,
                "discovery_message": message.id,
            }},
            upsert=True,
        )

        await interaction.response.send_message(
            f"✅ Survey posted, and {interaction.channel.mention} is now the discovery channel.\n"
            f"New members will be nudged here {PING_AFTER_DAYS} days after joining. "
            f"See `/discoveryhelp` for how it works.",
            ephemeral=True,
        )

    # ── /forcesurvey ─────────────────────────────────────────────────
    @app_commands.command(
        name="forcesurvey", description="Run today's nudge pass now instead of waiting for midday"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def force_survey(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.Client.mention_players()
        except Exception as e:
            await interaction.followup.send(f"❌ The nudge pass failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Ran the nudge pass. Any cohort that joined {PING_AFTER_DAYS} days ago has been "
            f"pinged — cohorts already pinged are skipped, so running this twice is harmless.",
            ephemeral=True,
        )

    # ── /discoveryhelp ───────────────────────────────────────────────
    @app_commands.command(
        name="discoveryhelp",
        description="How the new-member retention nudges work, and this server's status",
    )
    @app_commands.guild_only()
    async def discoveryhelp(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        try:
            server_data = await self._run(self._db["servers"].find_one,
                                          {"guild_id": guild.id}) or {}
            cohorts = await self._run(
                lambda: list(self._db["roles"].find({"guild_id": guild.id}))
            )
        except Exception as e:
            server_data, cohorts = {}, []
            print(f"[DiscoveryHelper] status lookup failed: {e}")

        embed = discord.Embed(
            title="🧭 Discovery Helper",
            description="Keeps server retention up by quietly nudging new members back "
                        "about a week after they join — right when they're most likely to "
                        "drift away.",
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(
            name="How it works",
            value=(
                "**1. Someone joins** → they get a role named after that day's date, e.g. "
                "`2026-08-04`. Everyone who joins the same day shares one role, so they form "
                "a *cohort*.\n\n"
                f"**2. {PING_AFTER_DAYS} days later** → around midday, the bot pings that "
                "cohort's role once in the discovery channel, then deletes the ping after "
                "2 seconds. They get the notification; the channel stays clean.\n\n"
                "**3. They come back** → the ping draws them to the survey asking how their "
                "experience has been, 1 to 10.\n\n"
                f"**4. Day {CLEANUP_AFTER_DAYS}** → the role and its record are deleted "
                "automatically, so roles never pile up."
            ),
            inline=False,
        )

        embed.add_field(
            name="Why it helps retention",
            value=(
                f"The nudge lands at the ~{PING_AFTER_DAYS}-day mark, which is where new "
                "members usually go quiet for good. Because it only pings one join-day cohort, "
                "it reaches a handful of people at a time instead of mass-pinging the server — "
                "so it stays useful rather than becoming noise people mute."
            ),
            inline=False,
        )

        channel = guild.get_channel(server_data.get("discovery_channel") or 0)
        pending = [c for c in cohorts if not c.get("mentioned")]
        done = len(cohorts) - len(pending)

        if channel is None:
            status = ("🔴 **Not set up.** Run `/setchannel` in the channel you want the survey "
                      "and nudges to live in.")
        else:
            status = f"🟢 **Active** — nudging in {channel.mention}"

        embed.add_field(name="Status", value=status, inline=False)

        if channel is not None:
            today = datetime.date.today()
            upcoming = []
            for c in sorted(pending, key=lambda x: x.get("date", "")):
                try:
                    joined = datetime.date.fromisoformat(c["date"])
                except (ValueError, KeyError, TypeError):
                    continue
                ping_on = joined + datetime.timedelta(days=PING_AFTER_DAYS)
                when = "today" if ping_on == today else (
                    f"in {(ping_on - today).days}d" if ping_on > today else "overdue")
                upcoming.append(f"`{c['date']}` → <@&{c['role_id']}> · {when}")

            embed.add_field(
                name="Cohorts",
                value=(f"**{len(pending)}** waiting to be nudged · **{done}** already done\n\n"
                       + ("\n".join(upcoming[:8]) if upcoming
                          else "*No cohorts waiting — nobody has joined recently.*")),
                inline=False,
            )
            missing = [n for n, ok in (
                ("Manage Roles", guild.me.guild_permissions.manage_roles),
                ("Send Messages", channel.permissions_for(guild.me).send_messages),
            ) if not ok]
            if missing:
                embed.add_field(
                    name="⚠️ Missing permissions",
                    value=f"I need **{', '.join(missing)}** or this can't work.",
                    inline=False)
                embed.color = COLOR_WARN

        embed.set_footer(text="/setchannel to set it up · /forcesurvey to run a pass now")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(client: commands.Bot):
    await client.add_cog(DiscoveryHelper(client))
    print("DiscoveryHelper cog loaded ✓")
