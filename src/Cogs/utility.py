import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from Brand import MINT

# <:name:id> or <a:name:id>. Anything else is either a plain unicode emoji, which every server
# already has, or not an emoji at all.
CUSTOM_EMOJI = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d+)>")
EMOJI_TIMEOUT = 10


class Utility(commands.Cog):
    """Utility commands: say, sync, emoji"""

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


    # ── Steal an emoji ─────────────────────────────────────────────
    @app_commands.command(name="emoji", description="Copy an emoji from another server")
    @app_commands.describe(emoji="Paste the emoji itself",
                           name="What to call it here. Defaults to its own name.")
    @app_commands.checks.cooldown(5, 60.0, key=lambda i: i.guild_id)
    @app_commands.default_permissions(manage_expressions=True)
    @app_commands.checks.has_permissions(manage_expressions=True)
    @app_commands.guild_only()
    async def emoji(self, interaction: discord.Interaction, emoji: str, name: str = None):
        found = CUSTOM_EMOJI.search(emoji or "")
        if not found:
            await interaction.response.send_message(
                "That isn't a custom emoji. Paste the emoji itself, from a server you're in. "
                "Plain ones like 😀 are already everywhere.", ephemeral=True)
            return

        animated, original, emoji_id = found.group(1) == "a", found.group(2), found.group(3)
        wanted = (name or original).strip().replace(" ", "_")[:32]
        if len(wanted) < 2 or not re.fullmatch(r"[A-Za-z0-9_]{2,32}", wanted):
            await interaction.response.send_message(
                "An emoji name has to be 2 to 32 letters, numbers or underscores.",
                ephemeral=True)
            return

        guild = interaction.guild
        # Animated and still emoji have separate allowances, and Discord only tells you which
        # one is full after the upload fails.
        used = sum(1 for e in guild.emojis if e.animated == animated)
        if used >= guild.emoji_limit:
            kind = "animated" if animated else "still"
            await interaction.response.send_message(
                f"This server is full on {kind} emoji, {used} of {guild.emoji_limit}. "
                f"Delete one or boost the server.", ephemeral=True)
            return
        if discord.utils.get(guild.emojis, name=wanted):
            await interaction.response.send_message(
                f"There's already one called `:{wanted}:` here. Pass a different `name`.",
                ephemeral=True)
            return

        await interaction.response.defer()
        url = (f"https://cdn.discordapp.com/emojis/{emoji_id}."
               f"{'gif' if animated else 'png'}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(
                        total=EMOJI_TIMEOUT)) as response:
                    if response.status != 200:
                        raise RuntimeError(f"cdn returned {response.status}")
                    image = await response.read()
        except Exception as e:
            print(f"[Utility] couldn't fetch emoji {emoji_id}: {e}")
            await interaction.followup.send(
                "Couldn't download that one. It may have been deleted.", ephemeral=True)
            return

        try:
            made = await guild.create_custom_emoji(
                name=wanted, image=image,
                reason=f"/emoji by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(
                "I need Manage Expressions to add emoji here.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Discord refused it: {exc}", ephemeral=True)
            return

        embed = discord.Embed(
            colour=MINT,
            description=f"{made} added as `:{made.name}:`"
                        + (f"\n-# it was called `:{original}:` where it came from"
                           if made.name != original else ""))
        embed.set_thumbnail(url=made.url)
        embed.set_footer(text=f"{used + 1} of {guild.emoji_limit} "
                              f"{'animated' if animated else 'still'} emoji used")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
