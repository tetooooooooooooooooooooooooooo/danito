import discord
from discord import app_commands
from discord.ext import commands

SYNC_ROLE_ID = 1123406231210565743

class Sync(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="sync", description="Sync slash commands")
    async def sync(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(SYNC_ROLE_ID)

        if not role or role not in interaction.user.roles:
            await interaction.response.send_message(
                "❌ You do not have permission to use this.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        synced = await self.bot.tree.sync(
            guild=discord.Object(id=interaction.guild.id)
        )

        await interaction.followup.send(
            f"✅ Synced {len(synced)} commands for this guild."
        )

async def setup(bot):
    await bot.add_cog(Sync(bot))
