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

# Right-click a message and Discord offers Copy Message Link, which is what most people have.
# Copy Message ID only appears with developer mode on, so accepting both is the difference
# between the option working first time and it working once somebody has read the docs.
MESSAGE_LINK = re.compile(
    r"(?:https?://)?(?:\w+\.)?discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)/(\d+)/?$")


def parse_message_ref(raw: str):
    """(channel_id, message_id) from a link, or (None, message_id) from a bare id.

    Returns (None, None) for anything else. A snowflake is taken as a string throughout: the
    option has to be a string parameter, because Discord sends integer options as JSON numbers
    and an id over 2^53 comes back from that having quietly lost its last digits.
    """
    text = (raw or "").strip()
    found = MESSAGE_LINK.search(text)
    if found:
        return int(found.group(1)), int(found.group(2))
    if text.isdigit() and 15 <= len(text) <= 20:
        return None, int(text)
    return None, None


def say_mentions(user, ping: bool = False) -> discord.AllowedMentions:
    """What the bot may ping on somebody's behalf.

    /say needs Manage Messages, which plenty of moderators hold and which does not include
    Mention Everyone. The bot has Mention Everyone, so without this a moderator could ping the
    whole server through the bot without holding the permission themselves. Mirrored off the
    person running the command rather than off the bot.

    replied_user is off unless it is asked for. A reply that pings is a decision, and the
    person being replied to did not ask to be pulled back into a thread by staff, so it is
    something you opt into per message rather than the default. Unlike everyone and roles it
    needs no permission of its own: it pings one person, in a thread they are already in, and
    anybody who can reply to them by hand can do the same thing without the bot.
    """
    perms = getattr(user, "guild_permissions", None)
    loud = bool(perms is not None and perms.mention_everyone)
    return discord.AllowedMentions(everyone=loud, roles=loud, users=True,
                                   replied_user=bool(ping))


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
    @app_commands.describe(
        message="The message to send. Optional if you're attaching a file.",
        reply="Optional. Reply to a message: paste its link, or its id if you have developer "
              "mode on.",
        ping="Whether the reply notifies the person you're replying to. Off unless you say so.",
        file="A file to post with it.",
        file2="A second file.",
        file3="A third file.")
    @app_commands.checks.cooldown(3, 30.0)
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, message: str = None,
                  reply: str = None, ping: bool = False,
                  file: discord.Attachment = None,
                  file2: discord.Attachment = None,
                  file3: discord.Attachment = None):
        # Three slots rather than one option taking many, because Discord has no such option:
        # an attachment option holds exactly one file, so more than one means more than one
        # option. Three is where the picker starts looking cluttered for a command that is
        # usually just text.
        attached = [a for a in (file, file2, file3) if a is not None]

        # `message` had to become optional to allow posting a file on its own, which means it
        # is now possible to ask for nothing at all.
        if not message and not attached:
            await interaction.response.send_message(
                "Give me something to say, a file to post, or both.", ephemeral=True)
            return

        # Nothing to ping without something to reply to, and silently ignoring it would leave
        # somebody believing they had sent a notification.
        if ping and not reply:
            await interaction.response.send_message(
                "`ping` only does anything alongside `reply`, since it decides whether the "
                "reply notifies the person you're answering. Add the message, or leave `ping` "
                "out.", ephemeral=True)
            return

        limit = getattr(interaction.guild, "filesize_limit", None)
        if limit and sum(a.size for a in attached) > limit:
            await interaction.response.send_message(
                f"That's more than this server's {limit // (1024 * 1024)}MB upload limit. "
                f"Discord let you attach it to the command, but I have to upload it again to "
                f"post it, and that upload is what the limit applies to.", ephemeral=True)
            return

        target = None
        if reply:
            channel_id, message_id = parse_message_ref(reply)
            if message_id is None:
                await interaction.response.send_message(
                    "I couldn't read that as a message. Right-click the message, Copy Message "
                    "Link, and paste that in. An id on its own works too.", ephemeral=True)
                return
            # Discord will only let a message reply to another one in the same channel, so a
            # link from elsewhere is refused here rather than by the API a moment later.
            if channel_id is not None and channel_id != interaction.channel.id:
                await interaction.response.send_message(
                    f"That message is in <#{channel_id}>. A reply has to be in the same "
                    f"channel as the message it answers, so run this there.", ephemeral=True)
                return
            try:
                target = await interaction.channel.fetch_message(message_id)
            except discord.NotFound:
                await interaction.response.send_message(
                    "There's no message with that id in this channel. It may have been "
                    "deleted, or the link may point somewhere else.", ephemeral=True)
                return
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I need **Read Message History** here to find that message.",
                    ephemeral=True)
                return
            except discord.HTTPException as exc:
                await interaction.response.send_message(
                    f"❌ Couldn't look that message up: {exc}", ephemeral=True)
                return

        # A reply to a message deleted between the lookup above and this send would otherwise
        # fail the whole thing, and saying it without the reply beats saying nothing. That
        # tolerance belongs to the reference, not to send(): passing fail_if_not_exists to
        # send() is a TypeError, and one that only shows up when somebody actually replies.
        reference = target.to_reference(fail_if_not_exists=False) if target else None

        # Every reply so far has been immediate, but downloading three files from the CDN is
        # not, and an interaction that says nothing for three seconds is dead. Everything past
        # this point answers through followup rather than response.
        files = []
        if attached:
            await interaction.response.defer(ephemeral=True)
            for a in attached:
                try:
                    # A spoilered attachment stays spoilered: it was marked that way on the way
                    # in and unwrapping it here would put it on screen unasked.
                    files.append(await a.to_file(spoiler=a.is_spoiler()))
                except discord.HTTPException as exc:
                    await interaction.followup.send(
                        f"❌ Couldn't fetch `{a.filename}`: {exc}", ephemeral=True)
                    return
        reply_to = (interaction.followup.send if attached
                    else interaction.response.send_message)

        try:
            await interaction.channel.send(
                message,
                files=files,
                reference=reference,
                allowed_mentions=say_mentions(interaction.user, ping))
        except discord.HTTPException as exc:
            await reply_to(f"❌ Failed to send: {exc}", ephemeral=True)
            return

        if target:
            # Which of the two it did matters to whoever ran it, because one of them put a
            # notification on somebody's phone and the other did not.
            done = (f"✅ Sent as a reply to {target.author.display_name}, and they were pinged."
                    if ping else "✅ Sent as a reply. Nobody was pinged.")
        else:
            done = "✅ Message sent."
        if files:
            done += f" {len(files)} file{'' if len(files) == 1 else 's'} attached."
        await reply_to(done, ephemeral=True)


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
