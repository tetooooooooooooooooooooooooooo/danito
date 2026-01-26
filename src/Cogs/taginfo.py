import discord
from discord import app_commands
from discord.ext import commands

class TagInfo(commands.Cog):
    """
    Displays information about a user's primary guild/server tag
    (the short tag + badge that appears next to their name globally).
    Example: MEOW with purple crown badge.
    Requires discord.py >= 2.6.0
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
        target = member or interaction.user

        await interaction.response.defer(ephemeral=False)

        try:
            user = await self.bot.fetch_user(target.id)
        except discord.NotFound:
            await interaction.followup.send("❌ User not found.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Failed to fetch user: {exc}", ephemeral=True)
            return

        primary = user.primary_guild  # PrimaryGuild object or None

        embed = discord.Embed(
            title=f"{target.display_name}'s Server Tag",
            color=discord.Color(0x5865F2),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"ID: {target.id}  •  Via Discord API")

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
            guild_id = primary.guild_id or "—"          # <-- FIXED: guild_id, not identity_guild_id
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
                # Guessed CDN (may need real pattern from dev tools inspection)
                badge_url = f"https://cdn.discordapp.com/guild-badges/{badge_hash}.png?size=64"
                embed.set_image(url=badge_url)
                embed.add_field(name="Badge", value="[Preview above]", inline=False)
            else:
                embed.add_field(name="Badge", value="None / Not set", inline=False)

            # Flavor for cat tags
            tag_upper = tag.upper()
            if tag_upper in ("MEOW", "NYAA", "PURR", "MROW", "KITTY"):
                embed.color = discord.Color(0xC47CFF)
                embed.description = "🐱 **Meow squad represent!** 😼✨"

        # Debug field
        show_debug = debug.value == "yes" if debug else False
        if show_debug:
            debug_text = (
                f"primary_guild exists? **{'Yes' if primary else 'No'}**\n"
                f"Type: {type(primary).__name__ if primary else 'None'}\n"
            )
            if primary:
                debug_text += (
                    f"guild_id: {primary.guild_id}\n"              # <-- FIXED
                    f"identity_enabled: {primary.identity_enabled}\n"
                    f"tag: {primary.tag}\n"
                    f"badge: {primary.badge}"
                )
            embed.add_field(
                name="Debug / Raw Data",
                value=f"```py\n{debug_text}```",
                inline=False
            )

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TagInfo(bot))
    print("TagInfo cog loaded ✓")
