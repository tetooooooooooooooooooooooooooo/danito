import discord
import re
from discord import app_commands
from discord.ext import commands

class ImageSpamFilter(commands.Cog):
    """Auto-deletes messages with 2+ attachments following a spam naming pattern"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = True
        self.min_attachments = 2
        self.image_ext = re.compile(r'\.(png|jpe?g|webp|gif)$', re.IGNORECASE)
        # Matches: 1.png, 2.jpg, 123.png — purely numeric filenames
        self.numeric_pattern = re.compile(r'^\d+\.(png|jpe?g|webp|gif)$', re.IGNORECASE)
        # Matches: image.png, photo.jpg, img.webp — generic single-word filenames
        self.generic_pattern = re.compile(r'^\w+\.(png|jpe?g|webp|gif)$', re.IGNORECASE)

    def is_spam_batch(self, attachments: list[discord.Attachment]) -> bool:
        filenames = [att.filename.lower() for att in attachments]

        # Rule 1: All share the exact same filename (image.png x4)
        if len(set(filenames)) == 1:
            return True

        # Rule 2: All are purely numeric names (1.png, 2.png, 3.png...)
        if all(self.numeric_pattern.match(f) for f in filenames):
            return True

        # Rule 3: All are sequential numeric (1.png, 2.png, 3.png — no gaps > 1)
        numeric = sorted([
            int(re.match(r'^(\d+)\.', f).group(1))
            for f in filenames
            if re.match(r'^(\d+)\.', f)
        ])
        if len(numeric) == len(filenames):
            gaps = [numeric[i+1] - numeric[i] for i in range(len(numeric)-1)]
            if all(g <= 1 for g in gaps):
                return True

        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enabled:
            return
        
        if message.guild is None or message.author.bot or not message.attachments:
            return
        
        # Only consider image attachments
        images = [att for att in message.attachments
                  if self.image_ext.search(att.filename)]
        
        if len(images) < self.min_attachments:
            return
        if len(images) != len(message.attachments):
            return

        if not self.is_spam_batch(images):
            return

        try:
            await message.delete()
            
            await message.channel.send(
                f"{message.author.mention} Message removed — Do Not Spam!!.",
                delete_after=20
            )

            filenames_str = ", ".join(att.filename for att in images)
            print(f"[ImageSpam] Deleted spam from {message.author} in {message.guild.name} ({message.jump_url})")
            
            await self.bot.send_log(
                title="Spam Message Deleted",
                fields={
                    "User": f"{message.author} ({message.author.id})",
                    "Guild": message.guild.name,
                    "Channel": message.channel.mention,
                    "Attachments": filenames_str,
                    "Message Content": message.content[:200] if message.content else "(no text)"
                },
                color=0xe74c3c
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
