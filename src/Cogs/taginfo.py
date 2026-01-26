import discord
from discord import app_commands
from discord.ext import commands

class TagInfo(commands.Cog):
    """
    Displays information about a user's primary guild/server tag
    (the short tag + badge that appears next to their name globally).
    Example: MEOW with purple crown badge.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="taginfo",
        description="Show a user's primary guild / server tag details"
    )
    @app_commands.describe(
        member="The user to check (defaults to yourself)",
        debug="Show raw/debug data? (for troubleshooting)"
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
        debug: app_commands.Choice[str] | None = None
    ):
        # Default to self
        target = member or interaction.user

        await interaction.response.defer(ephemeral=False)  # Give time to fetch

        try:
            # Fetch fresh user object (important for profile/identity data)
            user = await self.bot.fetch_user(target.id)
        except discord.NotFound:
            await interaction.followup.send("❌ User not found.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed to fetch user: {e}", ephemeral=True)
            return

        # Try to get primary_guild (may be None or dict-like)
        primary = getattr(user, "primary_guild", None)

        embed = discord.Embed(
            title=f"{target.display_name}'s Server Tag",
            color=discord.Color(0x5865F2),  # Discord blurple fallback
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"ID: {target.id}  •  Via Discord API")

        if not primary or not primary.get("identity_guild_id"):
            embed.color = discord.Color.greyple()
            embed.description = (
                "**No primary guild tag is currently set or visible.**\n\n"
                "This user doesn't have an active server tag/badge showing globally.\n"
                "• They may not have set a primary guild\n"
                "• The tag might be manually hidden\n"
                "• The guild no longer supports tags\n"
                "• Feature not available/rolled out to this account yet"
            )
        else:
            tag = primary.get("tag", "—")
            enabled = primary.get("identity_enabled", None)
            guild_id = primary.get("identity_guild_id", "—")
            badge_hash = primary.get("badge", None)

            # Status formatting
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
                # Attempt to guess badge CDN URL (common pattern in 2025–2026)
                # This may need updating — check Discord dev docs or network tab
                badge_url = f"https://cdn.discordapp.com/guild-badges/{badge_hash}.png?size=64"
                embed.set_image(url=badge_url)
                embed.add_field(name="Badge", value="[Preview above]", inline=False)
            else:
                embed.add_field(name="Badge", value="None / Not set", inline=False)

            # Flavor for popular tags
            tag_upper = tag.upper() if tag else ""
            if tag_upper in ("MEOW", "NYAA", "PURR", "MROW", "KITTY"):
                embed.color = discord.Color(0xC47CFF)  # purple-pink cat vibe
                embed.description = "🐱 **Meow squad represent!** 😼✨"
            elif tag_upper in ("LFG", "GG", "PRO", "GOD"):
                embed.color = discord.Color.gold()

        # Optional debug field
        show_debug = debug.value == "yes" if debug else False
        if show_debug:
            raw_str = f"primary_guild exists? **{'Yes' if primary else 'No'}**\n"
            if primary:
                raw_str += f"Raw keys: {', '.join(primary.keys())}"
            embed.add_field(
                name="Debug / Raw Data",
                value=f"```py\n{raw_str}```",
                inline=False
            )

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TagInfo(bot))
    print("TagInfo cog loaded ✓")
