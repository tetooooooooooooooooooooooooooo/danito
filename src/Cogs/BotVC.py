import discord
from discord import app_commands
from discord.ext import commands


class BotVC(commands.Cog):
    """Troll cog — /botvc makes the bot join your VC and just sit there.
    Run it again to make the bot leave.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="botvc", description="Bot joins your VC and sits there (run again to kick it out)")
    async def botvc(self, interaction: discord.Interaction):
        # Must be used in a guild
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        # Is the bot already sitting in a VC in this guild?
        voice_client = interaction.guild.voice_client

        if voice_client is not None and voice_client.is_connected():
            channel_name = voice_client.channel.name
            try:
                await voice_client.disconnect(force=True)
                print(f"[BotVC] Left voice channel '{channel_name}' in {interaction.guild.name}")
                await interaction.response.send_message(f"Left **{channel_name}**. 👋", ephemeral=True)
            except Exception as e:
                print(f"[BotVC] Error leaving VC: {e}")
                await interaction.response.send_message("Something went wrong trying to leave.", ephemeral=True)
            return

        # Not connected yet — figure out where to join
        author = interaction.user
        member = interaction.guild.get_member(author.id) or author

        target_channel = None
        if isinstance(member, discord.Member) and member.voice and member.voice.channel:
            target_channel = member.voice.channel

        if target_channel is None:
            await interaction.response.send_message(
                "You need to be in a voice channel first (or I don't know where to join).",
                ephemeral=True,
            )
            return

        try:
            await target_channel.connect()
            print(f"[BotVC] Joined voice channel '{target_channel.name}' in {interaction.guild.name}")
            await interaction.response.send_message(f"Joined **{target_channel.name}**. Just gonna sit here. 👀", ephemeral=True)
        except discord.Forbidden:
            print(f"[BotVC] Missing permissions to join '{target_channel.name}'")
            await interaction.response.send_message("I don't have permission to join that channel.", ephemeral=True)
        except Exception as e:
            print(f"[BotVC] Error joining VC: {e}")
            await interaction.response.send_message("Something went wrong trying to join.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BotVC(bot))
    print("BotVC cog loaded ✓")
