"""Per-server welcome and goodbye messages.

This replaces a hardcoded DM that went to every member of every server the bot was in,
advertising an unrelated game and linking to a different Discord. That was fine when the bot
ran in one server and is a good way to get terminated once it doesn't: unsolicited advertising
by DM is spam under Discord's terms, and server owners do not appreciate their new members
being pitched somewhere else.

Nothing is sent now unless a server sets it up, and what gets sent is that server's own words.

Two pieces of Discord behaviour decide when a welcome is actually welcome, and both of them
are about people who are not really here yet.

The first is membership screening, the same trivia AutoRole is built around. A server with a
rules screen still fires the join event immediately, but the member arrives *pending*: they
cannot post, usually cannot see the channel the welcome is going to, and plenty of them never
accept at all. So the welcome waits for the transition out of pending rather than the arrival.

The second is the account age gate. It kicks raid accounts on the way in, and it lives in the
join handler in Members. A listener here would have run alongside that handler rather than
after it, which meant a public welcome for somebody being removed a moment later. So there is
no join listener in this file: Members calls `greet` once the gate has let them through, and
tells us to swallow the goodbye when it has not.
"""

import datetime
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import GuildConfig
from Brand import MINT

MAX_MESSAGE = 1500          # comfortably inside both the 2000 content and 4096 embed limits
COLOR_WELCOME = MINT
COLOR_GOODBYE = 0xE67E22

PLACEHOLDERS = {
    "{user}": "mentions them, e.g. @someone",
    "{username}": "their display name",
    "{tag}": "their full username",
    "{server}": "this server's name",
    "{count}": "how many members there are now",
    "{ordinal}": "their position, e.g. 42nd",
    "\\n": "starts a new line",
}

# Custom text is rendered into a message the bot sends, so mass pings have to be impossible
# regardless of what an admin types.
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)
_MASS_PING = re.compile(r"@(everyone|here)", re.IGNORECASE)


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:          # 11th, 12th, 13th break the usual pattern
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def render(template: str, member: discord.Member, guild: discord.Guild) -> str:
    count = guild.member_count or 0
    # Slash command text inputs are single-line, so there is no way to type a real line break.
    # Writing \n is the only option available to somebody setting this up.
    out = (template
           .replace("\\n", "\n")
           .replace("{user}", member.mention)
           .replace("{username}", member.display_name)
           .replace("{tag}", str(member))
           .replace("{server}", guild.name)
           .replace("{count}", f"{count:,}")
           .replace("{ordinal}", _ordinal(count)))
    # Belt and braces alongside AllowedMentions: neutralise the text too, so a copied message
    # can't ping even if it is later reposted by something else.
    return _MASS_PING.sub(lambda m: "@​" + m.group(1), out)[:MAX_MESSAGE]


def _placeholder_help() -> str:
    return "\n".join(f"`{k}` {v}" for k, v in PLACEHOLDERS.items())


class Greetings(commands.Cog, name="Greetings"):
    """Welcome and goodbye messages, written by each server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # member id -> when the age gate turned them away, so the leave it causes stays quiet.
        self._turned_away: dict[int, float] = {}

    welcome = app_commands.Group(
        name="welcome", description="Greet people when they join",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
    goodbye = app_commands.Group(
        name="goodbye", description="Say something when people leave",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    # ── sending ──────────────────────────────────────────────────────
    def _build(self, text: str, member: discord.Member, guild: discord.Guild,
               as_embed: bool, color: int):
        body = render(text, member, guild)
        if not as_embed:
            return body, None
        embed = discord.Embed(description=body, color=color,
                              timestamp=discord.utils.utcnow())
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        return None, embed

    async def greet(self, member: discord.Member):
        """The welcome, called by Members once the age gate has let somebody in.

        Not a listener, deliberately. Listeners for the same event run alongside each other,
        so one here would race the gate and welcome people on their way back out.
        """
        if member.bot:
            return
        if member.pending:
            # Still on the rules screen, so they cannot see the channel this is going to and
            # may never accept. on_member_update below picks them up if they do.
            return
        await self._welcome(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Noisy event: every nickname, role and status change lands here. Leave at once unless
        # it is the one transition that matters, screening finished.
        if not (before.pending and not after.pending):
            return
        if after.bot:
            return
        await self._welcome(after)

    async def _welcome(self, member: discord.Member):
        cfg = await GuildConfig.get(self.bot, member.guild.id)
        if not cfg.get("welcome_enabled"):
            return
        text = cfg.get("welcome_message")
        if not text:
            return

        content, embed = self._build(
            text, member, member.guild, cfg.get("welcome_embed", False), COLOR_WELCOME)

        channel_id = cfg.get("welcome_channel")
        if channel_id:
            channel = member.guild.get_channel(channel_id)
            if channel is None:
                return
            try:
                await channel.send(content=content, embed=embed,
                                   allowed_mentions=SAFE_MENTIONS)
            except discord.Forbidden:
                print(f"[Greetings] can't post the welcome in {member.guild.id}")
            except discord.HTTPException as e:
                print(f"[Greetings] welcome send failed: {e}")
        else:
            # No channel configured means the server chose to greet them privately.
            try:
                await member.send(content=content, embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass          # plenty of people have DMs closed

    # ── people who were never really here ────────────────────────────
    # The age gate removes a raid account within a second of it arriving, and Discord reports
    # that as a member leaving like any other. Members says so on the way past and the id is
    # held just long enough for the remove event to catch up. A window rather than a flag
    # because the event may never arrive at all, and this must not grow forever.
    SUPPRESS_SECONDS = 60

    def suppress_goodbye(self, member_id: int):
        """Called by Members when the age gate turned somebody away."""
        now = time.monotonic()
        self._turned_away = {mid: at for mid, at in self._turned_away.items()
                             if now - at < self.SUPPRESS_SECONDS}
        self._turned_away[int(member_id)] = now

    def _was_turned_away(self, member_id: int) -> bool:
        at = self._turned_away.pop(int(member_id), None)
        return at is not None and time.monotonic() - at < self.SUPPRESS_SECONDS

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        if member.pending:
            # They never finished the rules screen, so they were never welcomed either. A
            # goodbye would be the only trace they ever left.
            return
        if self._was_turned_away(member.id):
            return
        cfg = await GuildConfig.get(self.bot, member.guild.id)
        if not cfg.get("goodbye_enabled"):
            return
        text = cfg.get("goodbye_message")
        channel_id = cfg.get("goodbye_channel")
        if not (text and channel_id):
            return
        channel = member.guild.get_channel(channel_id)
        if channel is None:
            return

        content, embed = self._build(
            text, member, member.guild, cfg.get("goodbye_embed", False), COLOR_GOODBYE)
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=SAFE_MENTIONS)
        except discord.Forbidden:
            print(f"[Greetings] can't post the goodbye in {member.guild.id}")
        except discord.HTTPException as e:
            print(f"[Greetings] goodbye send failed: {e}")

    # ── shared helpers ───────────────────────────────────────────────
    @staticmethod
    def _check_channel(guild: discord.Guild, channel: discord.TextChannel) -> Optional[str]:
        perms = channel.permissions_for(guild.me)
        missing = [n for n, ok in (("View Channel", perms.view_channel),
                                   ("Send Messages", perms.send_messages),
                                   ("Embed Links", perms.embed_links)) if not ok]
        return ", ".join(missing) or None

    async def _send_preview(self, interaction: discord.Interaction, header: str, text: str,
                            as_embed: bool, color: int, followup: bool = False):
        """Show the real rendered greeting, using whoever ran the command as the stand-in.

        The header and the preview have to be combined carefully: a plain-text greeting *is*
        the content, so it can't be passed alongside a header as a second content argument.
        """
        content, embed = self._build(text, interaction.user, interaction.guild, as_embed, color)
        if embed is not None:
            payload = {"content": header, "embed": embed}
        else:
            payload = {"content": f"{header}\n\n{content}"}

        send = interaction.followup.send if followup else interaction.response.send_message
        # none() so the preview renders the mention without actually pinging the admin.
        await send(ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **payload)

    # ── /welcome ─────────────────────────────────────────────────────
    @welcome.command(name="set", description="Write the message new members get")
    @app_commands.describe(
        message="What to say. Use {user} to mention them, {server} for the server name.",
        channel="Where to post it. Leave blank to send it as a direct message instead.",
        embed="Send it as an embed rather than plain text.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_set(self, interaction: discord.Interaction,
                          message: app_commands.Range[str, 1, MAX_MESSAGE],
                          channel: Optional[discord.TextChannel] = None,
                          embed: bool = False):
        if channel is not None:
            missing = self._check_channel(interaction.guild, channel)
            if missing:
                await interaction.response.send_message(
                    f"I'm missing **{missing}** in {channel.mention}. Grant those and try again.",
                    ephemeral=True)
                return

        await GuildConfig.update(self.bot, interaction.guild.id, {
            "welcome_enabled": True,
            "welcome_message": message,
            "welcome_channel": channel.id if channel else None,
            "welcome_embed": embed,
        })

        where = channel.mention if channel else "a direct message"
        await self._send_preview(
            interaction,
            f"Welcome messages are on, going to {where}. Here's how it'll look:",
            message, embed, COLOR_WELCOME)

    @welcome.command(name="off", description="Stop greeting new members")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_off(self, interaction: discord.Interaction):
        await GuildConfig.update(self.bot, interaction.guild.id, {"welcome_enabled": False})
        await interaction.response.send_message(
            "Welcome messages are off. Your wording is kept, so `/welcome set` isn't needed "
            "again unless you want to change it.", ephemeral=True)

    @welcome.command(name="show", description="See the current welcome message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_show(self, interaction: discord.Interaction):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        await self._show(interaction, cfg, "welcome", COLOR_WELCOME)

    # ── /goodbye ─────────────────────────────────────────────────────
    @goodbye.command(name="set", description="Write the message posted when someone leaves")
    @app_commands.describe(
        message="What to say. {user} still works, though they won't see it.",
        channel="Where to post it.",
        embed="Send it as an embed rather than plain text.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_set(self, interaction: discord.Interaction,
                          message: app_commands.Range[str, 1, MAX_MESSAGE],
                          channel: discord.TextChannel,
                          embed: bool = False):
        missing = self._check_channel(interaction.guild, channel)
        if missing:
            await interaction.response.send_message(
                f"I'm missing **{missing}** in {channel.mention}. Grant those and try again.",
                ephemeral=True)
            return

        await GuildConfig.update(self.bot, interaction.guild.id, {
            "goodbye_enabled": True,
            "goodbye_message": message,
            "goodbye_channel": channel.id,
            "goodbye_embed": embed,
        })
        await self._send_preview(
            interaction,
            f"Goodbye messages are on, going to {channel.mention}. Here's how it'll look:",
            message, embed, COLOR_GOODBYE)

    @goodbye.command(name="off", description="Stop posting when someone leaves")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_off(self, interaction: discord.Interaction):
        await GuildConfig.update(self.bot, interaction.guild.id, {"goodbye_enabled": False})
        await interaction.response.send_message(
            "Goodbye messages are off. Your wording is kept.", ephemeral=True)

    @goodbye.command(name="show", description="See the current goodbye message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_show(self, interaction: discord.Interaction):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        await self._show(interaction, cfg, "goodbye", COLOR_GOODBYE)

    # ── show, shared by both ─────────────────────────────────────────
    async def _show(self, interaction: discord.Interaction, cfg: dict, kind: str, color: int):
        enabled = bool(cfg.get(f"{kind}_enabled"))
        text = cfg.get(f"{kind}_message")
        channel_id = cfg.get(f"{kind}_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None

        embed = discord.Embed(
            title=f"{kind.title()} messages",
            description="**On**" if enabled else f"**Off.** Run `/{kind} set` to switch it on.",
            color=color if enabled else MINT,
        )
        if kind == "welcome":
            destination = channel.mention if channel else (
                "direct message" if text else "not set")
        else:
            destination = channel.mention if channel else "not set"
        embed.add_field(name="Goes to", value=destination, inline=True)
        embed.add_field(name="Style",
                        value="embed" if cfg.get(f"{kind}_embed") else "plain text", inline=True)
        embed.add_field(name="Your wording",
                        value=f"```\n{text[:900]}\n```" if text else "*nothing set yet*",
                        inline=False)
        embed.add_field(name="Placeholders you can use", value=_placeholder_help(), inline=False)

        if channel_id and channel is None:
            embed.add_field(
                name="Problem",
                value="The channel this was pointed at is gone, so nothing is being sent.",
                inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)
        if text:
            await self._send_preview(interaction, "Here's how it looks:", text,
                                     cfg.get(f"{kind}_embed", False), color, followup=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Greetings(bot))
    print("Greetings cog loaded ✓")
