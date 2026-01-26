import discord
from discord import app_commands
from discord.ext import commands
from collections import Counter

class TagInfo(commands.Cog):
    """
    Commands for primary guild / server tag information:
    • /taginfo @user     → shows details for one user
    • /guildtags         → shows summary of tags used in this server
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ────────────────────────────────────────────────
    # /taginfo
    # ────────────────────────────────────────────────
    @app_commands.command(name="taginfo", description="Show a user's primary guild / server tag details")
    @app_commands.describe(
        member="The user to check (defaults to yourself)",
        debug="Show detailed debug info?"
    )
    @app_commands.choices(
        debug=[
            app_commands.Choice(name="Yes", value="yes"),
            app_commands.Choice(name="No", value="no")
        ]
    )
    async def taginfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | discord.User | None = None,
        debug: str | None = None
    ):
        target = member or interaction.user

        await interaction.response.defer(ephemeral=False)

        try:
            user = await self.bot.fetch_user(target.id)
        except discord.NotFound:
            await interaction.followup.send("❌ User not found.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Fetch failed: {exc}", ephemeral=True)
            return

        primary = user.primary_guild

        embed = discord.Embed(
            title=f"{target.display_name}'s Server Tag",
            color=discord.Color(0x5865F2),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"ID: {target.id} • Via Discord API")

        if primary is None:
            embed.color = discord.Color.greyple()
            embed.description = (
                "**No primary guild tag is currently set or visible.**\n\n"
                "• No guild set as primary yet\n"
                "• Tag manually hidden\n"
                "• Guild no longer supports tags\n"
                "• Feature not rolled out to this account"
            )
        else:
            tag = primary.tag or "—"
            enabled = primary.identity_enabled
            guild_id = primary.identity_guild_id or "—"
            badge_hash = primary.badge or None

            if enabled is True:
                status = "✅ **Enabled** – visible everywhere"
            elif enabled is False:
                status = "❌ **Disabled** – manually hidden"
            else:
                status = "⚠️ **Null** – cleared by system"

            embed.add_field(name="Tag", value=f"**{tag}**", inline=True)
            embed.add_field(name="Status", value=status, inline=True)
            embed.add_field(name="Guild ID", value=str(guild_id), inline=True)

            if badge_hash:
                # This CDN pattern is a guess – test it
                badge_url = f"https://cdn.discordapp.com/guild-badges/{badge_hash}.png?size=64"
                embed.set_image(url=badge_url)
                embed.add_field(name="Badge", value="[Preview above]", inline=False)
            else:
                embed.add_field(name="Badge", value="None / Not set", inline=False)

            tag_upper = tag.upper()
            if tag_upper in ("MEOW", "NYAA", "PURR", "MROW", "KITTY"):
                embed.color = discord.Color(0xC47CFF)
                embed.description = "🐱 **Meow squad represent!** 😼✨"

        # Debug section
        show_debug = debug == "yes"
        if show_debug:
            debug_lines = [
                f"primary_guild exists? **{'Yes' if primary else 'No'}**",
                f"Type: {type(primary).__name__ if primary else 'None'}",
            ]
            if primary:
                attrs = dir(primary)
                debug_lines.append("All attributes on PrimaryGuild:")
                debug_lines.extend(f"  • {attr}" for attr in sorted(attrs) if not attr.startswith('_'))
                debug_lines.append("\nKnown fields (direct access):")
                debug_lines.append(f"  tag              : {primary.tag}")
                debug_lines.append(f"  identity_enabled : {primary.identity_enabled}")
                debug_lines.append(f"  identity_guild_id: {primary.identity_guild_id}")
                debug_lines.append(f"  badge            : {primary.badge}")

            embed.add_field(
                name="Debug / Raw Data",
                value="```py\n" + "\n".join(debug_lines) + "```",
                inline=False
            )

        await interaction.followup.send(embed=embed)

    # ────────────────────────────────────────────────
    # /guildtags
    # ────────────────────────────────────────────────
    @app_commands.command(
        name="guildtags",
        description="Show summary of primary guild/server tags in this server (online members by default)"
    )
    @app_commands.describe(
        online_only="Only scan online members? (recommended for large servers)",
        debug="Show debug stats?"
    )
    @app_commands.choices(
        online_only=[
            app_commands.Choice(name="Yes (fast)", value="yes"),
            app_commands.Choice(name="No - full server (slow!)", value="no")
        ],
        debug=[
            app_commands.Choice(name="Yes", value="yes"),
            app_commands.Choice(name="No", value="no")
        ]
    )
    async def guildtags(
        self,
        interaction: discord.Interaction,
        online_only: str = "yes",
        debug: str | None = None
    ):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer()

        if online_only == "yes":
            members = [m for m in guild.members if m.status != discord.Status.offline]
            scan_note = " (online members only – faster for large servers)"
        else:
            members = guild.members
            scan_note = " (full member list – may take minutes or timeout on 80k+ servers)"

        processed = len(members)
        tag_counts = Counter()
        total_with_tag = 0
        fetch_attempts = 0

        for member in members:
            primary = member.primary_guild

            if primary is None:
                fetch_attempts += 1
                try:
                    user = await self.bot.fetch_user(member.id)
                    primary = user.primary_guild
                except:
                    continue

            if primary and primary.tag:
                tag_counts[primary.tag] += 1
                total_with_tag += 1

        embed = discord.Embed(
            title=f"Primary Guild Tags in {guild.name}{scan_note}",
            description=f"**{total_with_tag}** members have a visible primary tag.\nProcessed **{processed}** / **{guild.member_count}** members.",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

        if tag_counts:
            top_tags = tag_counts.most_common(15)
            top_text = "\n".join(f"**{tag}**: {count}" for tag, count in top_tags)
            embed.add_field(name=f"Top Tags ({len(tag_counts)} unique)", value=top_text, inline=False)

            cat_tags = {t: c for t, c in tag_counts.items() if t.upper() in ("MEOW", "NYAA", "PURR", "MROW", "KITTY")}
            if cat_tags:
                cat_text = "\n".join(f"**{t}**: {c}" for t, c in sorted(cat_tags.items(), key=lambda x: x[1], reverse=True))
                embed.add_field(name="Cat Squad Tags 🐱", value=cat_text or "None", inline=False)

        else:
            embed.description += "\n\nNo primary guild tags detected in the scanned members."

        if debug == "yes":
            debug_info = (
                f"Processed members: {processed}/{guild.member_count}\n"
                f"Online-only mode: {online_only == 'yes'}\n"
                f"Fetch attempts: {fetch_attempts}\n"
                f"Unique tags: {len(tag_counts)}"
            )
            embed.add_field(name="Debug Info", value=f"```py\n{debug_info}```", inline=False)

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TagInfo(bot))
    print("TagInfo cog loaded ✓")
