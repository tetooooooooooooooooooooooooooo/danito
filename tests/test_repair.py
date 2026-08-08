"""Survey repair: detecting legacy buttons, in-place edit, startup migration, lazy repair."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, sys, types
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
    def count_documents(self, q): return len(list(self.find(q)))
    def update_one(self, q, ops, upsert=False):
        h = self._ref(q)
        if h is None:
            if not upsert: return types.SimpleNamespace(matched_count=0)
            h = dict(q); h.update(ops.get("$setOnInsert", {})); self.docs.append(h)
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

BOT_ID = 42
MSGS = {}


def row(*ids):
    return types.SimpleNamespace(children=[types.SimpleNamespace(custom_id=i) for i in ids])


class FakeMsg:
    def __init__(self, mid, components, author_id=BOT_ID):
        self.id = mid
        self.components = components
        self.author = types.SimpleNamespace(id=author_id)
        self.edits = []
    async def edit(self, **kw):
        self.edits.append(kw)
        self.components = [row(*[f"rating:{i}" for i in range(1, 11)])]
        return self


async def fetch(i):
    if i in MSGS:
        return MSGS[i]
    raise discord.NotFound(types.SimpleNamespace(status=404, reason=""), "nope")


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot._connection.user = types.SimpleNamespace(id=BOT_ID, name="soundcord", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Ratings")
    cog = bot.get_cog("Server Ratings")
    cog.upgrade_old_surveys.cancel()

    print("=== detecting readable buttons ===")
    legacy = FakeMsg(1, [row("a1b2-random", "c3d4-random")])
    modern = FakeMsg(2, [row(*[f"rating:{i}" for i in range(1, 11)])])
    assert cog._buttons_are_readable(legacy) is False
    assert cog._buttons_are_readable(modern) is True
    assert cog._buttons_are_readable(FakeMsg(3, [])) is False
    print("  legacy=False  modern=True  empty=False  OK")

    print("\n=== in-place repair ===")
    assert await cog._repair_survey(legacy) is True
    assert len(legacy.edits) == 1, legacy.edits
    kw = legacy.edits[0]
    assert kw["content"] is None, "old plain-text content should be cleared"
    assert kw["embed"] is not None and kw["view"] is not None
    ids = [c.custom_id for c in kw["view"].children]
    assert ids == [f"rating:{i}" for i in range(1, 11)], ids
    assert legacy.id == 1, "message id must not change"
    assert cog._buttons_are_readable(legacy) is True
    print(f"  edited in place, id still {legacy.id}, ids now rating:1..10 OK")

    other = FakeMsg(9, [row("x")], author_id=999)
    assert await cog._repair_survey(other) is False
    assert not other.edits, "must not edit a message we didn't author"
    print("  refuses to edit another author's message OK")

    print("\n=== startup migration ===")
    guilds = {}
    def mk(gid, cid, msg):
        MSGS[msg.id] = msg
        ch = types.SimpleNamespace(id=cid, fetch_message=fetch)
        guilds[gid] = types.SimpleNamespace(
            id=gid, get_channel=lambda i, c=ch, want=cid: c if i == want else None)

    m_old = FakeMsg(101, [row("rand-1")])
    m_new = FakeMsg(102, [row(*[f"rating:{i}" for i in range(1, 11)])])
    mk(1, 11, m_old)
    mk(2, 22, m_new)
    bot.get_guild = lambda gid: guilds.get(gid)

    DB["servers"].docs.extend([
        {"guild_id": 1, "discovery_channel": 11, "discovery_message": 101},
        {"guild_id": 2, "discovery_channel": 22, "discovery_message": 102},
        {"guild_id": 3, "discovery_channel": 33, "discovery_message": 103},   # deleted
    ])
    await cog.upgrade_old_surveys.coro(cog)
    assert len(m_old.edits) == 1, "stale survey should have been repaired"
    assert len(m_new.edits) == 0, "already-good survey should be left alone"
    print("  1 repaired, 1 already fine, 1 unreachable ignored OK")

    await cog.upgrade_old_surveys.coro(cog)
    assert len(m_old.edits) == 1, "second pass must not re-edit"
    print("  idempotent across restarts OK")

    print("\n=== lazy repair on a stale click ===")
    stale = FakeMsg(201, [row("rand-9")])
    MSGS[201] = stale
    # One settings document per guild, so repoint the existing one at the new survey.
    for d in DB["servers"].docs:
        if d["guild_id"] == 1:
            d["discovery_message"] = 201
    GuildConfig.invalidate(1)

    class Resp:
        def __init__(self): self.sent = None
        async def send_message(self, content=None, **k): self.sent = content

    i = types.SimpleNamespace(
        type=discord.InteractionType.component, message=stale,
        guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=11),
        user=types.SimpleNamespace(id=5), data={"custom_id": "rand-9"}, response=Resp())
    await cog.on_interaction(i)
    print(f"  reply: {i.response.sent}")
    assert len(stale.edits) == 1, "click should trigger a repair"
    assert "tap your number once more" in i.response.sent
    assert not DB["ratings"].docs, "unreadable click must not save a score"
    print("  repaired on click, asked them to tap again, nothing saved OK")

    # and now the retry actually lands
    i2 = types.SimpleNamespace(
        type=discord.InteractionType.component, message=stale,
        guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=11),
        user=types.SimpleNamespace(id=5), data={"custom_id": "rating:9"}, response=Resp())
    await cog.on_interaction(i2)
    print(f"  retry: {i2.response.sent}")
    assert DB["ratings"].docs and DB["ratings"].docs[0]["rating"] == 9
    print("  retry saved the score OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
