"""PingLog: only this bot's survey nudges are logged, with the cohort's reach."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, datetime, sys, types
sys.path.insert(0, SRC_DIR)


class FakeColl:
    def __init__(self): self.docs = []
    def create_index(self, *a, **k): pass
    def _ref(self, q): return next(iter(self.find(q)), None)
    def find_one(self, q, *a, **k):
        h = self._ref(q); return dict(h) if h else None
    def find(self, q=None, *a, **k):
        q = q or {}
        return [d for d in self.docs
                if all(d.get(k2) == v for k2, v in q.items() if not isinstance(v, dict))]
    def insert_one(self, d): self.docs.append(d)
    def update_one(self, q, ops, upsert=False):
        h = self._ref(q)
        if h is None:
            if not upsert: return types.SimpleNamespace(matched_count=0)
            h = dict(q); self.docs.append(h)
        h.update(ops.get("$set", {}))
        return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, FakeColl())


DB = FakeDB()
st = types.ModuleType("Database"); st.get_bot_database = lambda c: DB
sys.modules["Database"] = st
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = object
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import discord
from discord.ext import commands
import GuildConfig

GUILD, SURVEY_CHAN, LOGCHAN, BOT_ID, HUMAN = 1, 2, 3, 42, 500
SENT = []


def member(uid):
    return types.SimpleNamespace(id=uid, bot=False,
                                 display_avatar=types.SimpleNamespace(url="https://e.com/a.png"))


def role(name, n):
    return types.SimpleNamespace(id=900, name=name, members=[member(i) for i in range(1000, 1000 + n)])


class FakeChannel:
    def __init__(self, cid): self.id = cid; self.mention = f"<#{cid}>"
    async def send(self, **kw): SENT.append(kw)


GUILD_OBJ = types.SimpleNamespace(
    id=GUILD, member_count=250,
    get_channel=lambda i: FakeChannel(i) if i in (SURVEY_CHAN, LOGCHAN) else None,
    me=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(view_audit_log=True)))


def msg(*, content, roles=(), author_id=BOT_ID, channel_id=SURVEY_CHAN):
    a = member(author_id)
    return types.SimpleNamespace(
        id=9001, guild=GUILD_OBJ, channel=FakeChannel(channel_id), author=a,
        mentions=[], role_mentions=list(roles), mention_everyone=False,
        content=content, created_at=discord.utils.utcnow(),
        jump_url="https://discord.com/channels/1/2/9001")


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot._connection.user = types.SimpleNamespace(id=BOT_ID, name="soundcord", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.PingLog")
    cog = bot.get_cog("Ping Tracking")
    P = sys.modules["Cogs.PingLog"]

    print("=== commands ===")
    for c in bot.tree.walk_commands():
        params = [p.name for p in c.parameters]
        print(f"  /{c.qualified_name} {params} - {c.description}")
    # Switching this log on and off moved to /logging reminders, so this cog now has
    # listeners only. The recording below is what it is still responsible for.
    assert not list(bot.tree.walk_commands()), "PingLog should own no commands now"
    print("  no commands of its own, config lives under /logging OK")

    DB["servers"].docs.append({
        "guild_id": GUILD, "pinglog_enabled": True, "pinglog_channel": LOGCHAN,
        "discovery_channel": SURVEY_CHAN, "discovery_message": 555,
    })
    GuildConfig._cache.clear()

    cohort = role("2026-07-27", 12)

    print("\n=== what counts as a nudge ===")
    cases = [
        ("the real nudge", dict(content="<@&900>", roles=[cohort]), True),
        ("a human pinging the same role",
         dict(content="<@&900>", roles=[cohort], author_id=HUMAN), False),
        ("the bot pinging elsewhere",
         dict(content="<@&900>", roles=[cohort], channel_id=999), False),
        ("bot role ping with extra text",
         dict(content="<@&900> come rate us", roles=[cohort]), False),
        ("bot message, no role mention", dict(content="hello", roles=[]), False),
        ("two roles at once",
         dict(content="<@&900> <@&901>", roles=[cohort, role("other", 3)]), False),
    ]
    for label, kwargs, expected in cases:
        SENT.clear()
        await cog.on_message(msg(**kwargs))
        got = bool(SENT)
        assert got == expected, f"{label}: logged={got}, expected {expected}"
        print(f"  {'logged  ' if got else 'ignored '} {label}")

    print("\n=== the embed ===")
    SENT.clear(); DB["ping_events"].docs.clear()
    await cog.on_message(msg(content="<@&900>", roles=[cohort]))
    e = SENT[0]["embed"]
    print(f"  {e.title}: {e.description}")
    for f in e.fields:
        print(f"  [{f.name}] {f.value}")
    print(f"  footer: {e.footer.text}")
    assert e.title == "Survey reminder sent", e.title
    assert "**12**" in e.description
    vals = " ".join(f.value for f in e.fields)
    assert "2026-07-27" in vals, "the cohort date should be shown"
    assert len(e) <= 6000
    for f in e.fields:
        assert len(f.value) <= 1024
    assert SENT[0]["allowed_mentions"].everyone is False
    ev = DB["ping_events"].docs[0]
    assert ev["reach"] == 12 and ev["cohort"] == "2026-07-27", ev
    print("  reach, cohort date, stored event OK")

    print("\n=== an empty cohort ===")
    SENT.clear()
    await cog.on_message(msg(content="<@&900>", roles=[role("2026-08-01", 0)]))
    e = SENT[0]["embed"]
    assert "**0**" in e.description, e.description
    assert any("reached no one" in f.value for f in e.fields), "should call out a zero reach"
    print("  a nudge reaching nobody is still logged and flagged OK")

    print("\n=== survey channel not configured ===")
    DB["servers"].docs[0].pop("discovery_channel")
    GuildConfig._cache.clear()
    SENT.clear()
    await cog.on_message(msg(content="<@&900>", roles=[cohort]))
    assert not SENT, "with no survey channel there is nothing to match against"
    print("  nothing logged OK")

    print("\n=== tracking disabled ===")
    DB["servers"].docs[0]["discovery_channel"] = SURVEY_CHAN
    DB["servers"].docs[0]["pinglog_enabled"] = False
    GuildConfig._cache.clear()
    SENT.clear()
    await cog.on_message(msg(content="<@&900>", roles=[cohort]))
    assert not SENT
    print("  nothing logged OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
