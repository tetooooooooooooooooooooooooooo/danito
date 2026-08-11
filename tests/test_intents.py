"""Intents are minimal and toggleable; cooldowns exist and fire; check order is right."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, importlib, os, sys, types
sys.path.insert(0, SRC_DIR)

st = types.ModuleType("Database"); st.get_bot_database = lambda c: None
sys.modules["Database"] = st
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = lambda *a, **k: object()
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import discord
from discord import app_commands
from discord.ext import commands

SRC = open(str(ROOT / "src" / "main.py"), encoding="utf-8").read()
SRC = SRC.replace("bot = Bot()", "").replace('bot.run(os.environ.get("BOT_TOKEN"))', "")


def load_module():
    mod = types.ModuleType("botmain")
    exec(compile(SRC, "main.py", "exec"), mod.__dict__)
    return mod


def load_bot_class():
    return load_module().Bot


async def main():
    print("=== intents with presence on (default) ===")
    os.environ.pop("PRESENCE_INTENT", None)
    bot = load_bot_class()()
    i = bot.intents
    on = sorted(n for n, v in i if v)
    print(f"  enabled: {on}")
    # moderation carries bans and unbans, voice_states carries voice movement. Both feed the
    # server log and neither is privileged.
    assert on == ["guild_messages", "guilds", "members", "message_content", "moderation",
                  "presences", "voice_states"], on
    for off in ("typing", "guild_typing", "guild_reactions", "invites",
                "webhooks", "guild_scheduled_events", "auto_moderation_configuration",
                "dm_messages", "emojis_and_stickers"):
        assert not getattr(i, off), f"{off} should be off"
    print("  typing, reactions, invites, webhooks, DMs all off OK")

    # The number that decides whether this bot can grow past 100 servers. Adding a
    # non-privileged intent must never quietly move it.
    privileged = sorted(n for n in ("members", "message_content", "presences")
                        if getattr(i, n))
    assert privileged == ["members", "message_content", "presences"], privileged
    # Compared against discord.py's own bit values rather than a hardcoded list, so this keeps
    # meaning the right thing if Discord ever reclassifies one.
    privileged_mask = discord.Intents(
        members=True, presences=True, message_content=True).value
    for name in ("moderation", "voice_states"):
        assert discord.Intents(**{name: True}).value & privileged_mask == 0, \
            f"{name} must be non-privileged, or enabling it changes what Discord approves"
    print(f"  privileged in use: {', '.join(privileged)} (3 of 3), moderation is not one OK")
    await bot.close()

    print("\n=== PRESENCE_INTENT=0 drops it ===")
    os.environ["PRESENCE_INTENT"] = "0"
    bot2 = load_bot_class()()
    on2 = sorted(n for n, v in bot2.intents if v)
    print(f"  enabled: {on2}")
    assert "presences" not in on2, on2
    assert bot2.intents.members and bot2.intents.message_content, "the other two must stay"
    print("  presence off, members and message_content kept OK")
    await bot2.close()
    os.environ.pop("PRESENCE_INTENT", None)

    # ---- the stats commands must degrade rather than lie ----
    print("\n=== /stats degrades when presence is off ===")
    for mod in [m for m in sys.modules if m.startswith("Cogs.")]:
        del sys.modules[mod]
    b = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    b._connection.user = types.SimpleNamespace(id=42, name="soundcord", avatar=None)
    await b.load_extension("Cogs.stats")
    stats = b.get_cog("Stats")
    assert stats._has_presence() is False, "should report presence unavailable"

    class Resp:
        def __init__(self): self.sent = None; self._done = False
        def is_done(self): return self._done
        async def send_message(self, content=None, **kw): self.sent = content; self._done = True
        async def defer(self, **kw): self._done = True

    guild = types.SimpleNamespace(id=1, name="g", icon=None, member_count=10, members=[])
    inter = types.SimpleNamespace(guild=guild, response=Resp(), user=types.SimpleNamespace(
        id=7, display_name="t", display_avatar=types.SimpleNamespace(url="u")))
    await stats.playing.callback(stats, inter, online_only=True, show_examples=True)
    print(f"  /stats playing -> {inter.response.sent}")
    assert "presence intent is switched off" in inter.response.sent
    print("  says so instead of reporting 'nobody is playing' OK")

    # ---- cooldowns ----
    print("\n=== cooldowns ===")
    for mod in [m for m in sys.modules if m.startswith("Cogs.")]:
        del sys.modules[mod]
    b2 = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    b2._connection.user = types.SimpleNamespace(id=42, name="soundcord", avatar=None)
    b2.MongoClient = object()
    for c in ["Cogs.Ratings", "Cogs.Members", "Cogs.help", "Cogs.stats", "Cogs.utility",
              "Cogs.ImageSpamFilter", "Cogs.MediaLog", "Cogs.PingLog", "Cogs.Moderation"]:
        await b2.load_extension(c)

    # rate and per live in a closure, not on the command, so the cooldown is exercised
    # rather than inspected: run its predicate until it trips and read retry_after.
    async def trip(cmd, key=(1, 7)):
        """Run only the cooldown predicate. Returns retry_after once it refuses."""
        inter = types.SimpleNamespace(
            guild_id=key[0], user=types.SimpleNamespace(id=key[1]),
            created_at=discord.utils.utcnow())
        for chk in cmd.checks:
            try:
                await discord.utils.maybe_coroutine(chk, inter)
            except app_commands.CommandOnCooldown as e:
                return e.retry_after
            except Exception:
                pass          # permission checks aren't what's under test here
        return None

    expected = {
        "stats roles": (1, 20.0), "stats activity": (1, 60.0), "stats playing": (1, 20.0),
        "stats tags": (1, 120.0), "stats badges": (1, 20.0), "purge": (1, 5.0),
        "sync": (1, 300.0), "say": (3, 30.0), "forcesurvey": (1, 60.0),
        "ratings": (2, 20.0), "help": (2, 10.0),
    }
    by_name = {c.qualified_name: c for c in b2.tree.walk_commands()
               if isinstance(c, app_commands.Command)}

    for name, (rate, per) in expected.items():
        cmd = by_name[name]
        for n in range(rate):
            got = await trip(cmd)
            assert got is None, f"/{name} refused on use {n + 1} of {rate}"
        got = await trip(cmd)
        assert got is not None, f"/{name} never tripped after {rate} use(s)"
        assert abs(got - per) < 1.5, f"/{name} retry_after {got:.1f}s, expected ~{per}s"
        print(f"  /{name:16} {rate} per {per:.0f}s, tripped with {got:.0f}s to wait")

    print(f"  {len(expected)} cooldowns exercised OK")

    fresh = await trip(by_name["stats roles"], key=(1, 999))
    assert fresh is None, "one user's cooldown must not block another"
    print("  cooldowns are per user, not global OK")

    await trip(by_name["sync"], key=(50, 1))
    other_admin = await trip(by_name["sync"], key=(50, 2))
    assert other_admin is not None, "/sync should be shared across a guild"
    print("  /sync is shared per guild, so two admins can't double it OK")

    for name in ("ban", "kick", "timeout", "untimeout", "warn"):
        assert await trip(by_name[name]) is None, f"/{name} should not be rate limited"
        assert await trip(by_name[name]) is None, f"/{name} should not be rate limited"
    print("  ban/kick/timeout/untimeout/warn deliberately uncapped OK")

    print("\n=== every command run gets a line in the log ===")
    M = load_module()

    # Discord nests a subcommand's arguments one level down, so reading data["options"] gives
    # the subcommand rather than the arguments. This is the shape /logging media #chan arrives
    # in, and getting it wrong logs "media=None" instead of the channel.
    nested = {"name": "logging", "options": [
        {"name": "media", "type": 1, "options": [
            {"name": "channel", "type": 7, "value": "222"}]}]}
    flat = list(M._flatten_options(nested["options"]))
    assert [o["name"] for o in flat] == ["channel"], flat
    grouped = {"options": [
        {"name": "grp", "type": 2, "options": [
            {"name": "sub", "type": 1, "options": [
                {"name": "days", "type": 4, "value": 7}]}]}]}
    assert [o["name"] for o in M._flatten_options(grouped["options"])] == ["days"]
    assert list(M._flatten_options(None)) == [], "a command with no options logs no options"
    print("  subcommands and subcommand groups flattened to their real arguments OK")

    # Free text is reported as a length. A warn reason or the message behind /say is content,
    # the privacy policy says content is not written down, and a log is writing it down.
    for opt, want in (
        ({"name": "days", "value": 7}, "days=7"),
        ({"name": "on", "value": True}, "on=True"),
        ({"name": "rule", "value": "banned_words"}, "rule=banned_words"),
        ({"name": "when", "value": "1h30m"}, "when=1h30m"),
        ({"name": "channel", "value": "222"}, "channel=222"),
        ({"name": "reason", "value": "being rude in general"}, "reason=<21 chars>"),
        ({"name": "message", "value": "a" * 40}, "message=<40 chars>"),
        # Twenty one characters with no spaces is still too long to be a choice or an id.
        ({"name": "what", "value": "b" * 21}, "what=<21 chars>"),
    ):
        got = M._option_text(opt)
        assert got == want, (opt, got, want)
    print("  choices, durations and ids logged; anything typed logged as a length OK")

    print("\n=== and stdout is unbuffered, or none of it arrives ===")
    # Heroku pipes stdout, and a piped python block-buffers it: prints sit in a 4KB buffer and
    # whatever is in there when the dyno restarts is lost. This is the difference between a log
    # that lags and one that lies.
    procfile = (ROOT / "Procfile").read_text(encoding="utf-8")
    bot_line = next(l for l in procfile.splitlines() if l.startswith("node:"))
    assert " -u " in bot_line, f"the bot process needs python -u: {bot_line}"
    print(f"  {bot_line.strip()} OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
