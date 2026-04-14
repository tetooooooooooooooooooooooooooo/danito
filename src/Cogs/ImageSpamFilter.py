import discord
import re
from discord import app_commands
from discord.ext import commands

class ImageSpamFilter(commands.Cog):
    """Auto-deletes messages with 2+ attachments all named 'image.<ext>' (spam pattern)"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = True                     # default: active
        self.min_attachments = 2                # minimum number of image.<ext> to trigger
        self.spam_pattern = re.compile(r'^image\.(png|jpe?g|webp|gif)$', re.IGNORECASE)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enabled:
            return
        
        # Skip DMs, bots, no attachments
        if message.guild is None or message.author.bot or not message.attachments:
            return
        
        # Count how many match the image.<ext> pattern
        spam_count = sum(1 for att in message.attachments 
                        if self.spam_pattern.match(att.filename))
        
        # Trigger only if ALL attachments match AND count >= min
        if spam_count >= self.min_attachments and spam_count == len(message.attachments):
            try:
                await message.delete()
                
                # Optional short warning (auto-deletes after 20 seconds)
                await message.channel.send(
                    f"{message.author.mention} Message removed — Do Not Spam!!.",
                    delete_after=20
                )
                
                print(f"[ImageSpam] Deleted spam from {message.author} in {message.guild.name} ({message.jump_url})")
                
                # Log to the bot's logging channel
                await self.bot.send_log(
                    title="Spam Message Deleted",
                    fields={
                        "User": f"{message.author} ({message.author.id})",
                        "Guild": message.guild.name,
                        "Channel": message.channel.mention,
                        "Attachments": f"{spam_count} files matching image.<ext>",
                        "Message Content": message.content[:200] if message.content else "(no text)"
                    },
                    color=0xe74c3c  # Red color
                )
                
            except discord.Forbidden:
                print(f"[ImageSpam] No permission to delete in {message.guild.name}")
            except Exception as e:
                print(f"[ImageSpam] Error: {e}")
    
    @app_commands.command(
        name="toggleimagespam",
        description="Enable / disable the image spam filter"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def toggleimagespam(self, interaction: discord.Interaction):
        self.enabled = not self.enabled
        status = "**enabled**" if self.enabled else "**disabled**"
        await interaction.response.send_message(
            f"Image spam filter is now {status}.",
            ephemeral=True
        )
    
    @app_commands.command(
        name="imagespamstatus",
        description="Check if the image spam filter is currently active"
    )
    async def imagespamstatus(self, interaction: discord.Interaction):
        status = "active" if self.enabled else "disabled"
        await interaction.response.send_message(
            f"Image spam filter is currently **{status}**.",
            ephemeral=True
        )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageSpamFilter(bot))
    print("ImageSpamFilter cog loaded ✓")
