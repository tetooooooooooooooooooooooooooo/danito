import discord
from discord import app_commands
from discord.ext import commands

class TagInfo(commands.Cog):
    """
    Shows a user's primary guild / server tag info (e.g. MEOW tag + badge).
    Use debug: Yes to see detailed raw info for troubleshooting.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="taginfo",
        description="Show a user's primary guild / server tag details"
    )
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
                "• No guild set as primary\n"
                "• Tag manually hidden\n"
                "• Guild no longer supports tags\n"
                "• Feature not rolled out to this account"
            )
        else:
            # Safe attribute access
            tag = getattr(primary, 'tag', '—')
            enabled = getattr(primary, 'identity_enabled', None)
            guild_id = getattr(primary, 'identity_guild_id', getattr(primary, 'guild_id', '—'))
            badge = getattr(primary, 'badge', None)

            status = (
                "✅ Enabled – visible globally"
                if enabled is True else
                ("❌ Disabled – manually hidden"
                 if enabled is False else
                 "⚠️ Null – cleared by system")
            )

            embed.add_field(name="Tag", value=f"**{tag}**", inline=True)
            embed.add_field(name="Status", value=status, inline=True)
            embed.add_field(name="Guild ID", value=str(guild_id), inline=True)
            embed.add_field(name="Badge Hash", value=badge or "None", inline=False)

            if tag and tag.upper() in ("MEOW", "NYAA", "PURR", "MROW", "KITTY"):
                embed.color = discord.Color(0xC47CFF)
                embed.description = "🐱 **Meow squad represent!** 😼✨"

        # Expanded Debug Section
        show_debug = debug.value == "yes" if debug else False
        if show_debug:
            debug_lines = [
                f"primary_guild exists? **{'Yes' if primary else 'No'}**",
                f"Type: {type(primary).__name__ if primary else 'None'}",
            ]

            if primary:
                # List all available attributes
                attrs = dir(primary)
                debug_lines.append("All attributes on PrimaryGuild:")
                debug_lines.extend(f"  • {attr}" for attr in sorted(attrs) if not attr.startswith('_'))

                # Safe value checks for expected fields
                debug_lines.append("\nKnown fields (safe getattr):")
                debug_lines.append(f"  tag              : {getattr(primary, 'tag', 'Missing')}")
                debug_lines.append(f"  identity_enabled : {getattr(primary, 'identity_enabled', 'Missing')}")
                debug_lines.append(f"  identity_guild_id: {getattr(primary, 'identity_guild_id', 'Missing')}")
                debug_lines.append(f"  guild_id         : {getattr(primary, 'guild_id', 'Missing')}")
                debug_lines.append(f"  badge            : {getattr(primary, 'badge', 'Missing')}")

            embed.add_field(
                name="Debug / Raw Data (expanded)",
                value="```py\n" + "\n".join(debug_lines) + "```",
                inline=False
            )

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TagInfo(bot))
    print("TagInfo cog loaded ✓")
