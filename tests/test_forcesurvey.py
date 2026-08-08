"""mention_players: guild scoping, days offset, summary counts, cleanup opt-out."""
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
    def delete_many(self, q):
        gone = self.find(q)
        for d in gone: self.docs.remove(d)
        return types.SimpleNamespace(deleted_count=len(gone))


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
import os
os.environ["BOT_TOKEN"] = "x"

import discord

SENT = []


class FakeMsg:
    async def delete(self, delay=None): pass


class FakeChannel:
    def __init__(self, cid): self.id = cid
    async def send(self, content=None, **kw):
        SENT.append((self.id, content)); return FakeMsg()


class FakeGuild:
    def __init__(self, gid, cid): self.id = gid; self._cid = cid
    async def fetch_channel(self, i):
        if i == self._cid: return FakeChannel(i)
        raise discord.NotFound(types.SimpleNamespace(status=404, reason=""), "no")
    def get_role(self, rid): return None


def d(offset):
    return str((datetime.datetime.now() - datetime.timedelta(days=offset)).date())


async def main():
    # Import the Bot class without running it.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "botmain", str(ROOT / "src" / "main.py"))
    mod = importlib.util.module_from_spec(spec)
    src = open(str(ROOT / "src" / "main.py"), encoding="utf-8").read()
    src = src.replace("bot = Bot()", "").replace(
        'bot.run(os.environ.get("BOT_TOKEN"))', "")
    exec(compile(src, "main.py", "exec"), mod.__dict__)
    Bot = mod.Bot

    bot = Bot.__new__(Bot)          # skip __init__, we only need mention_players
    bot.MongoClient = object()
    guilds = {10: FakeGuild(10, 111), 20: FakeGuild(20, 222)}
    bot.fetch_guild = lambda gid: _fg(gid)
    async def _fg(gid): return guilds.get(gid)

    DB["servers"].docs.extend([
        {"guild_id": 10, "discovery_channel": 111},
        {"guild_id": 20, "discovery_channel": 222},
    ])

    print("=== guild scoping ===")
    DB["roles"].docs.clear(); SENT.clear()
    DB["roles"].docs.extend([
        {"_id": "a", "date": d(8), "role_id": 901, "guild_id": 10},
        {"_id": "b", "date": d(8), "role_id": 902, "guild_id": 20},
    ])
    s = await bot.mention_players(days=8, guild_id=10, cleanup=False)
    print(f"  summary: {s}")
    assert s["found"] == 1, s
    assert s["pinged"] == 1, s
    assert [c for c, _ in SENT] == [111], SENT
    assert DB["roles"].docs[1].get("mentioned") is None, "other guild must be untouched"
    print("  only the calling guild was nudged OK")

    print("\n=== no double nudge ===")
    SENT.clear()
    s = await bot.mention_players(days=8, guild_id=10, cleanup=False)
    print(f"  summary: {s}")
    assert s["pinged"] == 0 and s["already"] == 1, s
    assert not SENT, "already-nudged cohort must not ping again"
    print("  second run skipped it OK")

    print("\n=== nothing to do (the reported case) ===")
    DB["roles"].docs.clear(); SENT.clear()
    s = await bot.mention_players(days=8, guild_id=10, cleanup=False)
    print(f"  summary: {s}")
    assert s["found"] == 0 and s["pinged"] == 0, s
    assert not SENT
    print(f"  found nothing for {s['date']}, reported cleanly OK")

    print("\n=== days offset lets you test today's cohort ===")
    DB["roles"].docs.clear(); SENT.clear()
    DB["roles"].docs.append({"_id": "t", "date": d(0), "role_id": 999, "guild_id": 10})
    s = await bot.mention_players(days=8, guild_id=10, cleanup=False)
    assert s["found"] == 0, "today's cohort is not 8 days old"
    s = await bot.mention_players(days=0, guild_id=10, cleanup=False)
    print(f"  days=0 summary: {s}")
    assert s["found"] == 1 and s["pinged"] == 1, s
    assert SENT == [(111, "<@&999>")], SENT
    print("  days=0 nudged today's cohort OK")

    print("\n=== missing discovery channel ===")
    DB["roles"].docs.clear(); SENT.clear()
    DB["roles"].docs.append({"_id": "z", "date": d(8), "role_id": 5, "guild_id": 99})
    guilds[99] = FakeGuild(99, 555)
    s = await bot.mention_players(days=8, guild_id=99, cleanup=False)
    print(f"  summary: {s}")
    assert s["found"] == 1 and s["no_channel"] == 1 and s["pinged"] == 0, s
    print("  reported as no_channel rather than a silent failure OK")

    print("\n=== cleanup opt-out ===")
    DB["roles"].docs.clear()
    DB["roles"].docs.append({"_id": "old", "date": d(9), "role_id": 7, "guild_id": 10})
    s = await bot.mention_players(days=8, guild_id=10, cleanup=False)
    assert len(DB["roles"].docs) == 1, "cleanup=False must not delete anything"
    print("  cleanup=False left the 9-day-old record alone OK")
    s = await bot.mention_players(days=8, guild_id=10, cleanup=True)
    assert s["cleaned"] == 1, s
    assert not DB["roles"].docs, "cleanup=True should have removed it"
    print("  cleanup=True removed it and reported the count OK")

    print("\n=== global pass still covers every guild ===")
    DB["roles"].docs.clear(); SENT.clear()
    DB["roles"].docs.extend([
        {"_id": "g1", "date": d(8), "role_id": 801, "guild_id": 10},
        {"_id": "g2", "date": d(8), "role_id": 802, "guild_id": 20},
    ])
    s = await bot.mention_players(days=8, cleanup=False)
    print(f"  summary: {s}")
    assert s["pinged"] == 2, s
    assert sorted(c for c, _ in SENT) == [111, 222], SENT
    print("  scheduled pass nudged both guilds OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
