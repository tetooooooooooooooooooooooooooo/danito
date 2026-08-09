"""Posting the bot's own crashes to a channel.

Every check here is about the logger not making things worse. It is called from inside
exception handlers, so anything it raises arrives while something else is already broken, and
anything it repeats arrives once per occurrence of a fault that may be firing per message.

The interesting case is a bug in an event handler. One bad deploy can fire the same exception
thousands of times a minute, and a reporter without a limiter would post all of them, get rate
limited, and take the bot down with it.
"""
import pathlib as _pathlib
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")

import asyncio, sys, types
sys.path.insert(0, SRC_DIR)

for name in ("pymongo", "certifi", "dotenv"):
    mod = types.ModuleType(name)
    if name == "pymongo": mod.MongoClient = lambda *a, **k: object()
    if name == "certifi": mod.where = lambda: ""
    if name == "dotenv": mod.load_dotenv = lambda *a, **k: None
    sys.modules[name] = mod

import discord
import ErrorLog

CHANNEL, GUILD = 555, 777


class FakeChannel:
    def __init__(self, guild_id=GUILD, breaks=False):
        self.guild = types.SimpleNamespace(id=guild_id, name="Somewhere")
        self.sent = []
        self.breaks = breaks

    async def send(self, **kwargs):
        if self.breaks:
            raise discord.HTTPException(
                types.SimpleNamespace(status=403, reason=""), "no")
        self.sent.append(kwargs)


class FakeBot:
    def __init__(self, channel=None):
        self.channel = channel
        self.user = types.SimpleNamespace(name="Newt", display_avatar=types.SimpleNamespace(
            url="http://x/a.png"))

    def get_channel(self, cid):
        return self.channel if cid == CHANNEL else None


def boom(message="went wrong", kind=RuntimeError):
    """A real exception with a real traceback, since the signature reads the frames."""
    try:
        raise kind(message)
    except kind as e:
        return e


def boom_elsewhere(message="went wrong"):
    """Same type, different line. That is what makes it a different fault."""
    try:
        raise RuntimeError(message)
    except RuntimeError as e:
        return e


async def main():
    print("=== it posts an exception ===")
    channel = FakeChannel()
    log = ErrorLog.ErrorLog(FakeBot(channel), CHANNEL, GUILD)
    await log.report("on_member_join", boom("no such role"),
                     {"Guild": "Test Server (1)"})
    assert len(channel.sent) == 1, channel.sent
    embed = channel.sent[0]["embed"]
    assert "RuntimeError" in embed.title and "on_member_join" in embed.title, embed.title
    assert "no such role" in embed.description
    assert "```py" in embed.description, "the traceback wants to be readable"
    assert any(f.value == "Test Server (1)" for f in embed.fields)
    print(f"  {embed.title} OK")

    print("\n=== the same fault again stays quiet ===")
    # This is the one that matters. A broken on_message fires per message.
    for _ in range(500):
        await log.report("on_message", boom("same bug"))
    assert len(channel.sent) == 2, f"{len(channel.sent)} posts for 500 identical failures"
    print("  500 occurrences, 1 post OK")

    print("\n=== the message is deliberately not part of what makes it the same ===")
    # An exception whose text carries a user id or a channel name would otherwise look like a
    # brand new fault every single time, which defeats the whole limiter.
    before = len(channel.sent)
    await log.report("on_message", boom("same bug but about user 12345"))
    await log.report("on_message", boom("same bug but about user 67890"))
    assert len(channel.sent) == before, "the wording must not make it a new fault"
    print("  same type, same line, different wording: still one fault OK")

    print("\n=== a genuinely different fault is its own report ===")
    await log.report("on_message", boom_elsewhere("raised somewhere else"))  # other line
    await log.report("on_message", boom("same bug", kind=ValueError))        # other type
    await log.report("on_guild_join", boom("same bug"))                      # other event
    assert len(channel.sent) == before + 3, len(channel.sent)
    print("  a different line, a different type and a different event each report OK")

    print("\n=== and once the quiet spell is over it says how many it swallowed ===")
    quick = ErrorLog.ErrorLog(FakeBot(channel := FakeChannel()), CHANNEL, GUILD, cooldown=0)
    await quick.report("on_message", boom("noisy"))
    for _ in range(9):
        # cooldown=0 would report every one, so they are counted by hand against a signature
        # that is already known.
        quick._seen[quick.signature("on_message", boom("noisy"))][0] += 1
    await quick.report("on_message", boom("noisy"))
    latest = channel.sent[-1]["embed"]
    assert any("9" in f.value and "more time" in f.value for f in latest.fields), \
        [f.value for f in latest.fields]
    print("  the count of suppressed repeats comes with the next one OK")

    print("\n=== a giant traceback is cut from the front ===")
    channel = FakeChannel()
    log = ErrorLog.ErrorLog(FakeBot(channel), CHANNEL, GUILD)
    await log.report("on_message", boom("x" * 9000))
    embed = channel.sent[0]["embed"]
    assert len(embed.description) <= 4096, len(embed.description)
    assert embed.description.startswith("```py\n…"), "the end is the part that names the line"
    print(f"  {len(embed.description)} characters, within Discord's 4096 OK")

    print("\n=== a channel in the wrong guild is refused ===")
    # A traceback carries ids and message content. A mistyped channel id must not put that in
    # somebody else's server.
    elsewhere = FakeChannel(guild_id=999)
    log = ErrorLog.ErrorLog(FakeBot(elsewhere), CHANNEL, GUILD)
    await log.report("on_message", boom("secret"))
    assert not elsewhere.sent, "it posted into a guild it was not told to use"
    print("  nothing posted OK")

    # With no guild set at all it trusts the channel id, which is the opt-out.
    anywhere = FakeChannel(guild_id=999)
    log = ErrorLog.ErrorLog(FakeBot(anywhere), CHANNEL, guild_id=None)
    await log.report("on_message", boom("fine"))
    assert len(anywhere.sent) == 1
    print("  unless no guild was given, which is the way to switch that off OK")

    print("\n=== unconfigured does nothing at all ===")
    channel = FakeChannel()
    log = ErrorLog.ErrorLog(FakeBot(channel), channel_id=None)
    await log.report("on_message", boom("nowhere to go"))
    assert not channel.sent
    print("  no channel id, no posts, no crash OK")

    print("\n=== a channel it can't see is survivable ===")
    log = ErrorLog.ErrorLog(FakeBot(None), CHANNEL, GUILD)
    await log.report("on_message", boom("gone"))          # must not raise
    print("  says so on stdout and carries on OK")

    print("\n=== and a send that fails doesn't become a second error ===")
    broken = FakeChannel(breaks=True)
    log = ErrorLog.ErrorLog(FakeBot(broken), CHANNEL, GUILD)
    await log.report("on_message", boom("first"))         # must not raise
    print("  swallowed, because the caller is already handling a failure OK")

    print("\n=== nor does an error inside the reporter itself ===")
    class Exploding(FakeChannel):
        async def send(self, **kwargs):
            raise ZeroDivisionError("the logger is broken too")

    log = ErrorLog.ErrorLog(FakeBot(Exploding()), CHANNEL, GUILD)
    await log.report("on_message", boom("outer"))
    assert log._busy is False, "the guard has to clear, or nothing is ever reported again"
    print("  the reentry guard clears afterwards OK")

    print("\n=== what it remembers is bounded ===")
    channel = FakeChannel()
    log = ErrorLog.ErrorLog(FakeBot(channel), CHANNEL, GUILD)
    for n in range(ErrorLog.MAX_SIGNATURES * 2):
        log._due(f"fake-signature-{n}")
    assert len(log._seen) <= ErrorLog.MAX_SIGNATURES, len(log._seen)
    print(f"  {len(log._seen)} signatures kept of {ErrorLog.MAX_SIGNATURES * 2} seen OK")

    print("\n=== the bot wires it to the three places errors come from ===")
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    # Event listeners. discord.py calls Client.on_error directly, so it has to be a method on
    # the bot; a cog listener would never fire.
    assert "async def on_error(self, event_method" in source, "event errors go unreported"
    assert "self.errors.report" in source
    # Commands, and cogs that wouldn't load.
    assert source.count("self.errors.report") >= 3, "command and cog load errors too"
    assert "ERROR_CHANNEL_ID" in source, "it has to be configurable, not hardcoded"
    print("  on_error, on_tree_error and failed cog loads all report OK")

    print("\nALL CHECKS PASSED")


asyncio.run(main())
