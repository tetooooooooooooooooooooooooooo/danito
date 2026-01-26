import discord
from discord import app_commands
from discord.ext import commands

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    # Emoji mapping for cog categories
    COG_EMOJIS = {
        "Badges": "🏅",
        "Stats": "📊",
        "Help": "❓",
        "Commandcog": "⚙️",
        "Eventcog": "📅",
    }
    
    @app_commands.command(name="help", description="Show all bot commands")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 Bot Command List",
            description="All available commands for this bot",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        
        # Group commands by cog
        commands_by_cog = {}
        for cmd in self.bot.tree.walk_commands():
            if isinstance(cmd, app_commands.Group):
                continue  # Skip command groups for now
            
            cog_name = cmd.binding.__class__.__name__ if cmd.binding else "Other"
            commands_by_cog.setdefault(cog_name, []).append(cmd)
        
        # Sort cogs alphabetically
        for cog_name in sorted(commands_by_cog.keys()):
            cmds = commands_by_cog[cog_name]
            
            # Get emoji for this cog or use default
            emoji = self.COG_EMOJIS.get(cog_name, "▫️")
            
            # Build command list
            cmd_list = []
            for cmd in sorted(cmds, key=lambda x: x.name):
                # Format: /command - description
                cmd_list.append(f"`/{cmd.name}` • {cmd.description or 'No description'}")
            
            # Add field to embed
            embed.add_field(
                name=f"{emoji} {cog_name}",
                value="\n".join(cmd_list) if cmd_list else "No commands",
                inline=False
            )
        
        # Add footer with bot info
        embed.set_footer(
            text=f"Total Commands: {len([cmd for cmds in commands_by_cog.values() for cmd in cmds])} | Requested by {interaction.user.name}",
            icon_url=interaction.user.display_avatar.url
        )
        
        # Set thumbnail (optional - use bot's avatar)
        if self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)
        
        # Send publicly (remove ephemeral=True)
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Help(bot))
