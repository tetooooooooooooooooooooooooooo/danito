"""Posting the bot's own crashes into a Discord channel.

Everything that goes wrong currently goes to stdout, which means reading Heroku logs, which
means noticing there was something to read. A channel is where you already are.

Three things make this harder than it sounds, and all three are why it lives in its own file
rather than as four lines in main:

- **It must never raise.** A logger that throws turns one bug into two, and the second one is
  in the thing meant to tell you about the first.
- **It must never loop.** Reporting an error is itself an operation that can fail, and doing
  that inside the handler for a failed operation is how a bot fills a channel in ten seconds.
- **It must not repeat itself.** A broken `on_message` handler fires once per message. Without
  a limiter, one bad deploy posts thousands of identical embeds and gets the bot rate limited
  into uselessness, which also stops it doing its actual job.
"""

import datetime
import time
import traceback

import discord

COLOR = 0xF27272

# How long the same fault stays quiet after being reported once. Long enough that a per
# message failure reports once rather than continuously, short enough to notice it recurring.
COOLDOWN = 300

# Discord's embed description is capped at 4096, and a traceback is worth nothing truncated
# from the wrong end: the last frames are the ones naming the line that broke.
MAX_TRACE = 3600
MAX_SIGNATURES = 500        # ceiling on what is remembered, so it can't grow forever


class ErrorLog:
    """Reports exceptions to one channel, at most once per fault per cooldown."""

    def __init__(self, bot, channel_id=None, guild_id=None, cooldown: int = COOLDOWN):
        self.bot = bot
        self.channel_id = channel_id
        # When set, the channel has to be in this guild. A mistyped id would otherwise post
        # stack traces, which carry ids and message content, into somebody else's server.
        self.guild_id = guild_id
        self.cooldown = cooldown
        # signature -> [suppressed since last report, when last reported]
        self._seen = {}
        # Guards against reporting a failure that happened inside a report.
        self._busy = False

    # ── deciding whether to say anything ─────────────────────────────
    @staticmethod
    def signature(where: str, exc: BaseException) -> str:
        """What counts as "the same error again".

        The type and where it came from, plus the deepest frame, which is the line that
        actually broke. Deliberately not the message: an exception whose text carries a user
        id or a channel name would otherwise look like a new fault every time.
        """
        spot = ""
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            last = frames[-1]
            spot = f"{last.filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{last.lineno}"
        return f"{where}|{type(exc).__name__}|{spot}"

    def _due(self, signature: str):
        """(whether to send, how many were swallowed since the last time)."""
        now = time.monotonic()
        record = self._seen.get(signature)
        if record is None:
            if len(self._seen) >= MAX_SIGNATURES:
                # Drop the coldest half rather than clearing, so a long running fault that is
                # still recurring keeps its place.
                for key in sorted(self._seen, key=lambda k: self._seen[k][1])[:MAX_SIGNATURES // 2]:
                    del self._seen[key]
            self._seen[signature] = [0, now]
            return True, 0
        suppressed, last = record
        if now - last < self.cooldown:
            record[0] = suppressed + 1
            return False, 0
        record[0], record[1] = 0, now
        return True, suppressed

    # ── saying it ────────────────────────────────────────────────────
    async def report(self, where: str, exc: BaseException, context: dict = None):
        """Post one exception. Silent, and safe, when it can't."""
        if not self.channel_id or self._busy:
            return
        try:
            send, suppressed = self._due(self.signature(where, exc))
            if not send:
                return

            channel = self.bot.get_channel(self.channel_id)
            if channel is None:
                print(f"[errors] channel {self.channel_id} isn't visible to me")
                return
            if self.guild_id and getattr(channel, "guild", None) \
                    and channel.guild.id != self.guild_id:
                print(f"[errors] channel {self.channel_id} is in guild "
                      f"{channel.guild.id}, not {self.guild_id}. Refusing to post there.")
                return

            self._busy = True
            await channel.send(embed=self._embed(where, exc, context, suppressed))
        except Exception as e:
            # The whole point of this class. Whatever went wrong reporting, the caller is
            # already handling a failure and does not need a second one.
            print(f"[errors] couldn't report {where}: {type(e).__name__}: {e}")
        finally:
            self._busy = False

    def _embed(self, where: str, exc: BaseException, context: dict, suppressed: int):
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(trace) > MAX_TRACE:
            # Kept from the end. The first frames are this file and discord.py's dispatcher;
            # the last one is the line that broke.
            trace = "…\n" + trace[-MAX_TRACE:]

        embed = discord.Embed(
            title=f"{type(exc).__name__} in {where}"[:256],
            description=f"```py\n{trace}\n```",
            colour=COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc))

        for name, value in (context or {}).items():
            if value is not None:
                embed.add_field(name=name, value=str(value)[:1024], inline=True)

        if suppressed:
            embed.add_field(
                name="Also",
                value=f"This happened **{suppressed}** more time"
                      f"{'' if suppressed == 1 else 's'} in the last "
                      f"{self.cooldown // 60} minutes and wasn't reported each time.",
                inline=False)
        embed.set_footer(text=f"Quiet about this one for {self.cooldown // 60} minutes")
        return embed
