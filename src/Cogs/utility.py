import discord
from discord import app_commands
from discord.ext import commands


class Utility(commands.Cog):
    """Utility commands: say, sync"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Sync slash commands ────────────────────────────────────────
    @app_commands.command(name="sync", description="Force-refresh this server's slash commands")
    async def sync(self, interaction: discord.Interaction):
        # Gated only by the bot-wide manage_guild interaction check — any admin in any
        # server can use this, which matters now that the bot isn't confined to one guild.
        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            synced = await self.bot.tree.sync(
                guild=discord.Object(id=interaction.guild.id)
            )
            await interaction.followup.send(
                f"✅ Synced **{len(synced)}** command{'s' if len(synced) != 1 else ''} "
                f"to this server.\n"
                f"-# If a command still looks missing or outdated in the picker, fully "
                f"close and reopen Discord — the client caches the command list locally "
                f"and doesn't always refresh it on its own.",
                ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Failed to sync commands: {exc}",
                ephemeral=True
            )

    # ── Say ────────────────────────────────────────────────────────
    @app_commands.command(name="say", description="Make the bot send a message")
    @app_commands.describe(message="The message to send")
    async def say(self, interaction: discord.Interaction, message: str):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ You need **Manage Messages** permission to use this command.",
                ephemeral=True
            )
            return

        # You can also add: if message is too long / contains @everyone etc.
        # but keeping it simple for now

        try:
            await interaction.channel.send(message)
            await interaction.response.send_message("✅ Message sent.", ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"❌ Failed to send: {exc}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
