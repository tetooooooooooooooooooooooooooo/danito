import discord
from discord import app_commands
from discord.ext import commands


class Utility(commands.Cog):
    """Utility commands: say, sync"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Sync slash commands ────────────────────────────────────────
    @app_commands.command(name="sync", description="Force-refresh this server's slash commands")
    # Keyed per guild, not per user: Discord's command-update budget is shared, so two admins
    # taking turns would burn through it just as fast as one.
    @app_commands.checks.cooldown(1, 300.0, key=lambda i: i.guild_id)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def sync(self, interaction: discord.Interaction):
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
                f"close and reopen Discord. The client caches the command list locally "
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
    @app_commands.checks.cooldown(3, 30.0)
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, message: str):
        try:
            await interaction.channel.send(message)
            await interaction.response.send_message("✅ Message sent.", ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"❌ Failed to send: {exc}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
