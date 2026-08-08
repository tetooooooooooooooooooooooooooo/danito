"""Per-server welcome and goodbye messages.

This replaces a hardcoded DM that went to every member of every server the bot was in,
advertising an unrelated game and linking to a different Discord. That was fine when the bot
ran in one server and is a good way to get terminated once it doesn't: unsolicited advertising
by DM is spam under Discord's terms, and server owners do not appreciate their new members
being pitched somewhere else.

Nothing is sent now unless a server sets it up, and what gets sent is that server's own words.

A separate listener from the one in Members is deliberate. The pair that were merged earlier
were two halves of the same job doing overlapping database writes; this is a distinct feature
with its own settings, and it reads them from the shared config cache, so the extra listener
costs a dictionary lookup.
"""

import datetime
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import GuildConfig

MAX_MESSAGE = 1500          # comfortably inside both the 2000 content and 4096 embed limits
COLOR_WELCOME = 0x2ECC71
COLOR_GOODBYE = 0xE67E22
COLOR_INFO = 0x5865F2

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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
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

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
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

    async def _preview(self, interaction: discord.Interaction, text: str,
                       as_embed: bool, color: int) -> dict:
        """Rendered against whoever ran the command, so they can see the real thing."""
        content, embed = self._build(text, interaction.user, interaction.guild, as_embed, color)
        return {"content": content, "embed": embed}

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
        preview = await self._preview(interaction, message, embed, COLOR_WELCOME)
        await interaction.response.send_message(
            f"Welcome messages are on, going to {where}. Here's how it'll look:",
            ephemeral=True, **preview)

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
        preview = await self._preview(interaction, message, embed, COLOR_GOODBYE)
        await interaction.response.send_message(
            f"Goodbye messages are on, going to {channel.mention}. Here's how it'll look:",
            ephemeral=True, **preview)

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
            color=color if enabled else COLOR_INFO,
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

        payload = {"embed": embed}
        await interaction.response.send_message(ephemeral=True, **payload)
        if text:
            preview = await self._preview(interaction, text, cfg.get(f"{kind}_embed", False),
                                          color)
            await interaction.followup.send(content=preview["content"],
                                            embed=preview["embed"], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Greetings(bot))
    print("Greetings cog loaded ✓")
