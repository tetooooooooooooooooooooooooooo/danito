import discord
from discord import app_commands
from discord.ext import commands

class TagInfo(commands.Cog):
    """
    Cog for the /taginfo command – shows a user's primary guild/server tag
    (e.g. the 'MEOW' tag with badge that appears next to names globally).
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="taginfo",
        description="Display a user's primary guild / server tag information"
    )
    @app_commands.describe(
        member="The user to check (leave blank for yourself)"
    )
    async def taginfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | discord.User | None = None
    ):
        # Default to the command invoker if no member is provided
        target = member or interaction.user

        # Fetch fresh user data to ensure we have profile/identity fields
        try:
            user = await self.bot.fetch_user(target.id)
        except discord.NotFound:
            await interaction.response.send_message("❌ User not found.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"❌ Failed to fetch user data: {exc}", ephemeral=True
            )
            return

        # Access primary_guild (may be None or a dict-like structure)
        primary = getattr(user, "primary_guild", None)

        embed = discord.Embed(
            title=f"{target.display_name}'s Server Tag Info",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"User ID: {target.id} • Fetched via Discord API")

        if not primary or not primary.get("identity_guild_id"):
            embed.description = "No primary guild / server tag is currently set or visible."
            embed.color = discord.Color.greyple()
            await interaction.response.send_message(embed=embed)
            return

        # Extract fields (using .get() for safety)
        tag           = primary.get("tag",          "—")
        enabled       = primary.get("identity_enabled", None)
        guild_id      = primary.get("identity_guild_id", "—")
        badge_hash    = primary.get("badge",        "—")

        # Format status nicely
        if enabled is True:
            status = "✅ Enabled (visible globally)"
        elif enabled is False:
            status = "❌ Disabled (hidden by user)"
        else:
            status = "⚠️ Null / Cleared by system"

        embed.add_field(name="Tag",           value=f"**{tag}**", inline=True)
        embed.add_field(name="Status",        value=status,      inline=True)
        embed.add_field(name="Guild ID",      value=str(guild_id), inline=True)
        embed.add_field(name="Badge Hash",    value=badge_hash or "None", inline=False)

        # Little flavor for popular tags
        if tag and tag.upper() in ("MEOW", "NYAA", "PURR", "RAW R"):
            embed.color = discord.Color(0xC47CFF)  # cute purple
            embed.set_footer(text=f"🐱 {tag} squad checking in! • {embed.footer.text}")

        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TagInfo(bot))
