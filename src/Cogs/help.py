import discord
from discord import app_commands
from discord.ext import commands

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all bot commands")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🤖 Bot Help",
            description="Here are all available slash commands:",
            color=discord.Color.blurple()
        )

        commands_by_cog = {}

        for cmd in self.bot.tree.walk_commands():
            cog = cmd.binding.__class__.__name__ if cmd.binding else "Other"
            commands_by_cog.setdefault(cog, []).append(cmd)

        for cog_name, cmds in commands_by_cog.items():
            value = "\n".join(
                f"/{cmd.name} — {cmd.description or 'No description'}"
                for cmd in cmds
            )
            embed.add_field(
                name=cog_name,
                value=value,
                inline=False
            )

        embed.set_footer(text="Use /<command> to run a command")
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Help(bot))
