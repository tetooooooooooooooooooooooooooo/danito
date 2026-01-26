import discord
from discord import app_commands
from discord.ext import commands

class ImageSpamFilter(commands.Cog):
    """Auto-deletes messages with 2+ attachments all named 'image.png' (spam pattern)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = True                     # default: active
        self.min_attachments = 2                # minimum number of image.png to trigger
        self.target_filename = "image.png"      # case-insensitive check

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enabled:
            return

        # Skip DMs, bots, no attachments
        if message.guild is None or message.author.bot or not message.attachments:
            return

        # Count how many are exactly "image.png" (case-insensitive)
        spam_count = sum(1 for att in message.attachments if att.filename.lower() == self.target_filename.lower())

        # Trigger only if ALL attachments match AND count >= min
        if spam_count >= self.min_attachments and spam_count == len(message.attachments):
            try:
                await message.delete()
                # Optional short warning (auto-deletes after 10 seconds)
                await message.channel.send(
                    f"{message.author.mention} Message removed — avoid posting multiple default-named screenshots.",
                    delete_after=10
                )
                print(f"[ImageSpam] Deleted spam from {message.author} in {message.guild.name} ({message.jump_url})")
            except discord.Forbidden:
                print(f"[ImageSpam] No permission to delete in {message.guild.name}")
            except Exception as e:
                print(f"[ImageSpam] Error: {e}")

    @app_commands.command(
        name="toggleimagespam",
        description="Enable / disable the image.png spam filter"
    )
    @app_commands.default_permissions(manage_guild=True)  # Only Manage Server users can run this
    async def toggleimagespam(self, interaction: discord.Interaction):
        self.enabled = not self.enabled
        status = "**enabled**" if self.enabled else "**disabled**"
        await interaction.response.send_message(
            f"Image spam filter is now {status}.",
            ephemeral=True
        )

    @app_commands.command(
        name="imagespamstatus",
        description="Check if the image.png spam filter is currently active"
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
