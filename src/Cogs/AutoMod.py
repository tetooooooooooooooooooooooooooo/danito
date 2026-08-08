"""Automatic moderation: nine rules, each switched on separately, each with its own response.

The thing that decides whether an automod is useful or hated is false positives. A rule that
deletes a moderator's message, or eats a link somebody needed, costs more trust than the spam
it stopped. So the defaults are all off, every rule is independent, and the exemptions are
checked before any rule runs: staff are skipped, chosen roles and channels are skipped, and
anyone the bot could not moderate by hand is skipped too.

Actions escalate: delete the message, delete and warn, delete and time them out, delete and
kick, delete and ban. The last two can't be undone by clicking something, so there is a limit
on how many can happen in an hour. Past it they become timeouts, which are reversible, and the
case says why. A wrong entry on a banned word list otherwise empties a server before anybody
notices, and that is not a theoretical way for an automod to go wrong.

Every action falls back rather than giving up. A ban the bot has no permission for becomes a
timeout; a timeout it can't place is still written down as a warning. The alternative is a
rule that looks switched on and quietly does nothing.

Warnings and timeouts are recorded through the Moderation cog, so an automod action lands in
the same numbered case history as one a moderator took by hand. Somebody looking up a member
sees the whole picture rather than half of it.
"""

import asyncio
import datetime
import re
import time
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import GuildConfig

# (key, icon, label). Mirrored in the dashboard's store.AUTOMOD_RULES; a test asserts the two
# agree, since a typo means a rule the dashboard can switch on and the bot never runs.
RULES = [
    ("words",      "🚫", "Banned words"),
    ("invites",    "✉️", "Discord invites"),
    ("links",      "🔗", "Links"),
    ("mentions",   "📣", "Mass mentions"),
    ("spam",       "💬", "Message flood"),
    ("duplicates", "♻️", "Repeated messages"),
    ("caps",       "🔠", "Shouting"),
    ("emoji",      "😀", "Emoji spam"),
    ("newlines",   "📜", "Wall of text"),
]
RULE_KEYS = [key for key, _, _ in RULES]

ACTIONS = ("delete", "warn", "timeout", "kick", "ban")
ACTION_WORDS = {
    "delete": "delete the message",
    "warn": "delete it and record a warning",
    "timeout": "delete it and time them out",
    "kick": "delete it and kick them",
    "ban": "delete it and ban them",
}
# The two that can't be undone by clicking something.
REMOVALS = ("kick", "ban")

# Everything off until a server turns it on, and the thresholds set where a reasonable person
# would put them rather than where they catch the most.
DEFAULTS = {
    "words":      {"on": False, "action": "delete", "list": []},
    "invites":    {"on": False, "action": "delete"},
    "links":      {"on": False, "action": "delete", "allow": []},
    "mentions":   {"on": False, "action": "warn", "limit": 5},
    "spam":       {"on": False, "action": "timeout", "count": 6, "seconds": 5},
    "duplicates": {"on": False, "action": "delete", "count": 3},
    "caps":       {"on": False, "action": "delete", "percent": 70, "min_length": 12},
    "emoji":      {"on": False, "action": "delete", "limit": 8},
    "newlines":   {"on": False, "action": "delete", "limit": 15},
}

DEFAULT_TIMEOUT_MINUTES = 10

# A brake on the two actions nobody can undo with a click. A wrong word on the banned list, or
# a rule that turns out to match more than anybody expected, otherwise empties a server before
# a human notices. Past the limit, removals fall back to a timeout, which is reversible, and
# the case says why. Raise it if you would rather it never got in the way.
DEFAULT_MAX_REMOVALS = 5
REMOVAL_WINDOW = 3600
MAX_WORDS = 100
MAX_WORD_LENGTH = 40
MAX_DOMAINS = 50

# How much history the flood and repeat rules keep per person, and how long a notice stays up.
HISTORY = 12
HISTORY_TTL = 120
NOTICE_SECONDS = 6
PRUNE_MINUTES = 10
WORD_CACHE_SIZE = 64      # distinct banned word lists kept compiled

COLOR_INFO = 0x5865F2
COLOR_WARN = 0xE67E22
COLOR_GOOD = 0x2ECC71

INVITE = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|dsc\.gg|invite\.gg)/[\w-]+",
    re.IGNORECASE)
URL = re.compile(r"https?://([^\s/?#]+)", re.IGNORECASE)
CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
UNICODE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
# Zero width characters, which are how a filter gets walked straight past.
INVISIBLE = re.compile(r"[​-‏⁠﻿­]")


def _settings(cfg: dict, key: str) -> dict:
    """A rule's settings with the defaults filled in, so a partial document still works."""
    stored = ((cfg.get("automod") or {}).get("rules") or {}).get(key) or {}
    return {**DEFAULTS[key], **stored}


def _normalise(text: str) -> str:
    return INVISIBLE.sub("", text or "").lower()


class AutoMod(commands.Cog, name="AutoMod"):
    """Rules that act on messages without waiting for a moderator."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id) -> deque of (when, normalised content)
        self._recent: dict[tuple, deque] = {}
        # The banned word list compiled once per distinct list rather than per message.
        self._word_cache: dict[tuple, re.Pattern] = {}
        # guild_id -> when each automatic kick or ban happened, for the hourly brake.
        self._removals: dict[int, deque] = {}

    automod = app_commands.Group(
        name="automod", description="Rules that act on messages by themselves",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    async def cog_load(self):
        self.prune.start()

    async def cog_unload(self):
        self.prune.cancel()

    @tasks.loop(minutes=PRUNE_MINUTES)
    async def prune(self):
        """Drop history for people who stopped talking, so the dict can't grow forever."""
        cutoff = time.monotonic() - HISTORY_TTL
        for key in [k for k, v in self._recent.items() if not v or v[-1][0] < cutoff]:
            self._recent.pop(key, None)

    # ── the rules ────────────────────────────────────────────────────
    def _words_pattern(self, words: list) -> Optional[re.Pattern]:
        clean = tuple(sorted({w.strip().lower() for w in words if w and w.strip()}))
        if not clean:
            return None
        if clean not in self._word_cache:
            # Whole words only. Substring matching is how a filter for "ass" starts eating
            # "class" and "passive", and nobody forgives that twice.
            joined = "|".join(re.escape(w) for w in clean)
            if len(self._word_cache) >= WORD_CACHE_SIZE:
                self._word_cache.clear()   # cheap ceiling; these rebuild in microseconds
            self._word_cache[clean] = re.compile(rf"(?<!\w)(?:{joined})(?!\w)")
        return self._word_cache[clean]

    def _check_words(self, message, s):
        pattern = self._words_pattern(s.get("list") or [])
        if pattern is None:
            return None
        hit = pattern.search(_normalise(message.content))
        return f"used a banned word ({hit.group(0)})" if hit else None

    @staticmethod
    def _check_invites(message, s):
        return "posted a Discord invite" if INVITE.search(message.content or "") else None

    @staticmethod
    def _check_links(message, s):
        allow = {d.strip().lower().removeprefix("www.")
                 for d in (s.get("allow") or []) if d.strip()}
        for match in URL.finditer(message.content or ""):
            domain = match.group(1).lower().removeprefix("www.")
            # Subdomains of an allowed domain are allowed too, so one entry covers a site.
            if any(domain == a or domain.endswith("." + a) for a in allow):
                continue
            return f"posted a link ({domain})"
        return None

    @staticmethod
    def _check_mentions(message, s):
        total = len(set(message.mentions)) + len(set(message.role_mentions))
        limit = int(s.get("limit", 5))
        return (f"mentioned {total} people or roles at once" if total > limit else None)

    @staticmethod
    def _check_caps(message, s):
        content = message.content or ""
        letters = [c for c in content if c.isalpha()]
        if len(letters) < int(s.get("min_length", 12)):
            return None
        shouted = sum(1 for c in letters if c.isupper()) / len(letters) * 100
        limit = int(s.get("percent", 70))
        return f"wrote {shouted:.0f}% in capitals" if shouted >= limit else None

    @staticmethod
    def _check_emoji(message, s):
        count = (len(CUSTOM_EMOJI.findall(message.content or ""))
                 + len(UNICODE_EMOJI.findall(message.content or "")))
        limit = int(s.get("limit", 8))
        return f"used {count} emoji in one message" if count > limit else None

    @staticmethod
    def _check_newlines(message, s):
        lines = (message.content or "").count("\n")
        limit = int(s.get("limit", 15))
        return f"posted {lines + 1} lines in one message" if lines > limit else None

    def _check_spam(self, message, s, history):
        window = float(s.get("seconds", 5))
        count = int(s.get("count", 6))
        cutoff = time.monotonic() - window
        recent = sum(1 for when, _ in history if when >= cutoff)
        return (f"sent {recent} messages in {window:.0f} seconds"
                if recent >= count else None)

    def _check_duplicates(self, message, s, history):
        content = _normalise(message.content)
        if not content:
            return None
        count = int(s.get("count", 3))
        same = sum(1 for _, text in history if text == content)
        return f"posted the same message {same} times" if same >= count else None

    # ── exemptions ───────────────────────────────────────────────────
    def _exempt(self, message, automod) -> bool:
        member = message.author
        if message.channel.id in set(automod.get("exempt_channels") or []):
            return True
        exempt_roles = set(automod.get("exempt_roles") or [])
        if exempt_roles and any(r.id in exempt_roles for r in member.roles):
            return True
        if automod.get("exempt_staff", True):
            perms = message.channel.permissions_for(member)
            if perms.manage_messages or perms.manage_guild or perms.administrator:
                return True
        # Somebody the bot could not action by hand either. Acting here would half work: the
        # message goes and the timeout silently fails.
        me = message.guild.me
        if member.id == message.guild.owner_id or member.top_role >= me.top_role:
            return True
        return False

    # ── the listener ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        # A webhook post has no member behind it, so there is nobody to warn or time out and
        # its author carries no roles to check exemptions against.
        if message.webhook_id is not None:
            return

        cfg = await GuildConfig.get(self.bot, message.guild.id)
        automod = cfg.get("automod") or {}
        if not automod.get("enabled"):
            return
        if self._exempt(message, automod):
            return

        # History is recorded before the checks, because the flood and repeat rules count this
        # message too, and after the exemptions, so staff chatter isn't kept at all.
        key = (message.guild.id, message.author.id)
        history = self._recent.setdefault(key, deque(maxlen=HISTORY))
        history.append((time.monotonic(), _normalise(message.content)))

        for rule in RULE_KEYS:
            s = _settings(cfg, rule)
            if not s.get("on"):
                continue
            if rule == "spam":
                reason = self._check_spam(message, s, history)
            elif rule == "duplicates":
                reason = self._check_duplicates(message, s, history)
            else:
                reason = getattr(self, f"_check_{rule}")(message, s)
            if reason:
                await self._act(message, rule, reason, s.get("action", "delete"), automod)
                return          # one rule per message, so nobody gets three punishments at once

    # ── acting on it ─────────────────────────────────────────────────
    def _removal_allowed(self, guild_id: int, limit: int) -> bool:
        """Whether another kick or ban is within the hourly limit, counting it if so."""
        now = time.monotonic()
        seen = self._removals.setdefault(guild_id, deque())
        while seen and seen[0] < now - REMOVAL_WINDOW:
            seen.popleft()
        if len(seen) >= limit:
            return False
        seen.append(now)
        return True

    async def _act(self, message, rule: str, reason: str, action: str, automod: dict):
        guild, member = message.guild, message.author
        label = next(lbl for key, _, lbl in RULES if key == rule)

        try:
            await message.delete()
        except discord.NotFound:
            pass                        # somebody else got there first
        except discord.Forbidden:
            print(f"[AutoMod] no Manage Messages in {guild.id}")
            return
        except discord.HTTPException as e:
            print(f"[AutoMod] delete failed in {guild.id}: {e}")

        minutes = int(automod.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES)
        limit = int(automod.get("max_removals") or DEFAULT_MAX_REMOVALS)
        note = ""

        # A kick or a ban that can't go ahead becomes a timeout rather than nothing at all,
        # so the person is still stopped and the case says what really happened.
        if action in REMOVALS and not self._removal_allowed(guild.id, limit):
            note = (f" Automatic {action}s are paused: {limit} in the last hour is the limit, "
                    f"so this was a timeout instead.")
            print(f"[AutoMod] removal limit of {limit}/hour reached in {guild.id}")
            action = "timeout"

        outcome = await self._carry_out(guild, member, action, label, minutes)
        if outcome != action and action in REMOVALS:
            note = (f" I couldn't {action} them, so this was a "
                    f"{'timeout' if outcome == 'timeout' else 'warning'} instead.")

        if automod.get("notify", True):
            await self._notify(message, member, label, outcome, minutes)

        if outcome != "delete":
            await self._record_case(guild, member, outcome, reason + note,
                                    minutes if outcome == "timeout" else None)

    async def _carry_out(self, guild, member, action: str, label: str, minutes: int) -> str:
        """Do it, and report what actually happened rather than what was asked for.

        Every branch falls back rather than giving up: a ban the bot can't place still times
        the person out, and a timeout it can't place is still written down as a warning. The
        alternative is a rule that silently does nothing at all.
        """
        reason = f"AutoMod: {label}"
        perms = guild.me.guild_permissions

        if action == "ban":
            if perms.ban_members:
                try:
                    await guild.ban(member, reason=reason)
                    return "ban"
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"[AutoMod] ban failed in {guild.id}: {e}")
            else:
                print(f"[AutoMod] no Ban Members in {guild.id}")
            action = "timeout"

        elif action == "kick":
            if perms.kick_members:
                try:
                    await member.kick(reason=reason)
                    return "kick"
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"[AutoMod] kick failed in {guild.id}: {e}")
            else:
                print(f"[AutoMod] no Kick Members in {guild.id}")
            action = "timeout"

        if action == "timeout":
            if perms.moderate_members:
                try:
                    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
                    await member.timeout(until, reason=reason)
                    return "timeout"
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"[AutoMod] timeout failed in {guild.id}: {e}")
            else:
                print(f"[AutoMod] no Moderate Members in {guild.id}")
            return "warn"

        return action

    async def _notify(self, message, member, label, outcome, minutes):
        """A short note in the channel that clears itself up.

        Without it people repost the same thing three times wondering why it vanished.
        """
        text = f"{member.mention} that was removed automatically ({label.lower()})."
        if outcome == "timeout":
            text += f" You're muted for {minutes} minutes."
        elif outcome == "kick":
            text = f"**{member}** was removed from the server automatically ({label.lower()})."
        elif outcome == "ban":
            text = f"**{member}** was banned automatically ({label.lower()})."
        try:
            await message.channel.send(
                text, delete_after=NOTICE_SECONDS,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False,
                                                         everyone=False))
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _record_case(self, guild, member, action, reason, minutes):
        """Log it through the Moderation cog so it lands in the same case history.

        Going through that cog rather than writing to the collection here means one place
        decides what a case looks like and where it gets posted.
        """
        mod = self.bot.get_cog("Moderation")
        if mod is None:
            return
        me = guild.me
        try:
            case_id = await mod._record(
                guild.id, action, member.id, str(member), me.id, f"{me} (AutoMod)",
                f"AutoMod: {reason}", (minutes * 60) if minutes else None)
            await mod._post_case(guild, case_id, action, member, me,
                                 f"AutoMod: {reason}",
                                 duration=(minutes * 60) if minutes else None)
        except Exception as e:
            print(f"[AutoMod] couldn't record the case in {guild.id}: {e}")

    # ── commands ─────────────────────────────────────────────────────
    @automod.command(name="on", description="Switch automod on")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def on(self, interaction: discord.Interaction):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        automod = dict(cfg.get("automod") or {})
        live = [k for k in RULE_KEYS if _settings(cfg, k).get("on")]
        automod["enabled"] = True
        await GuildConfig.update(self.bot, interaction.guild.id, {"automod": automod})

        if live:
            await interaction.response.send_message(
                f"Automod is on, with {len(live)} rule{'' if len(live) == 1 else 's'} "
                f"running. `/automod status` shows them.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "Automod is on, but no rules are switched on yet so nothing will happen. "
                "Turn some on with `/automod rule`, or on the dashboard.", ephemeral=True)

    @automod.command(name="off", description="Switch automod off")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        automod = dict(cfg.get("automod") or {})
        automod["enabled"] = False
        await GuildConfig.update(self.bot, interaction.guild.id, {"automod": automod})
        await interaction.response.send_message(
            "Automod is off. Your rules are kept, so `/automod on` brings them all back.",
            ephemeral=True)

    @automod.command(name="rule", description="Switch one rule on or off")
    @app_commands.describe(rule="Which rule.", on="On or off.",
                           action="What to do when it catches something.")
    @app_commands.choices(
        rule=[app_commands.Choice(name=f"{icon} {label}", value=key)
              for key, icon, label in RULES],
        action=[app_commands.Choice(name="Delete the message", value="delete"),
                app_commands.Choice(name="Delete and warn", value="warn"),
                app_commands.Choice(name="Delete and time them out", value="timeout"),
                app_commands.Choice(name="Delete and kick them", value="kick"),
                app_commands.Choice(name="Delete and ban them", value="ban")])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rule(self, interaction: discord.Interaction,
                   rule: app_commands.Choice[str], on: bool,
                   action: Optional[app_commands.Choice[str]] = None):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        automod = dict(cfg.get("automod") or {})
        rules = dict(automod.get("rules") or {})
        current = _settings(cfg, rule.value)
        current["on"] = on
        if action is not None:
            current["action"] = action.value
        rules[rule.value] = current
        automod["rules"] = rules
        if on:
            automod["enabled"] = True
        await GuildConfig.update(self.bot, interaction.guild.id, {"automod": automod})

        if not on:
            await interaction.response.send_message(
                f"**{rule.name}** is off.", ephemeral=True)
            return

        extra = ""
        if rule.value == "words" and not current.get("list"):
            extra = " There are no words on the list yet, so add some on the dashboard."
        if rule.value == "links" and not current.get("allow"):
            extra = " Every link will be removed. Add allowed sites on the dashboard."
        await interaction.response.send_message(
            f"**{rule.name}** is on, and will {ACTION_WORDS[current['action']]}.{extra}",
            ephemeral=True)

    @automod.command(name="status", description="See which rules are running")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        automod = cfg.get("automod") or {}
        guild = interaction.guild

        on_lines, off_labels = [], []
        for key, icon, label in RULES:
            s = _settings(cfg, key)
            if s.get("on"):
                on_lines.append(f"{icon} **{label}** · {ACTION_WORDS[s['action']]}")
            else:
                off_labels.append(label)

        enabled = bool(automod.get("enabled"))
        embed = discord.Embed(
            title="Automod",
            color=COLOR_GOOD if enabled and on_lines else COLOR_INFO,
            description=("**On**" if enabled else
                         "**Off.** `/automod on` starts it, or set it up on the dashboard."))

        embed.add_field(name=f"Running ({len(on_lines)})",
                        value="\n".join(on_lines) or "*no rules on yet*", inline=False)
        if off_labels:
            embed.add_field(name=f"Not running ({len(off_labels)})",
                            value=", ".join(off_labels), inline=False)

        exempt = []
        roles = [guild.get_role(r) for r in (automod.get("exempt_roles") or [])]
        roles = [r.mention for r in roles if r]
        channels = [guild.get_channel(c) for c in (automod.get("exempt_channels") or [])]
        channels = [c.mention for c in channels if c]
        if automod.get("exempt_staff", True):
            exempt.append("anyone who can manage messages")
        if roles:
            exempt.append(", ".join(roles))
        if channels:
            exempt.append(", ".join(channels))
        embed.add_field(name="Never touched",
                        value="\n".join(f"· {e}" for e in exempt) or "*nobody*", inline=False)
        embed.set_footer(text="Warnings and timeouts appear in the moderation log as cases.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
    print("AutoMod cog loaded ✓")
