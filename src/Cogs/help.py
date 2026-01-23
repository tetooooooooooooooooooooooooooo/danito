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
            description="Here are all available commands:",
            color=discord.Color.blurple()
        )

        for cog in self.bot.cogs.values():
            commands_list = []
            for cmd in cog.get_app_commands():
                commands_list.append(f"/{cmd.name} — {cmd.description}")

            if commands_list:
                embed.add_field(
                    name=cog.qualified_name,
                    value="\n".join(commands_list),
                    inline=False
                )

        embed.set_footer(text="Use /<command> to run a command")

        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Help(bot))
