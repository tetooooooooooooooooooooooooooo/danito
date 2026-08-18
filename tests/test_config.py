"""GuildConfig: shared caching, read-count reduction, invalidation, stale fallback, indexes."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio
import time, sys, types
sys.path.insert(0, SRC_DIR)

READS = []
INDEXES = []


class FakeColl:
    def __init__(self, name): self.name = name; self.docs = []
    def create_index(self, keys, **kw):
        INDEXES.append((self.name, keys, kw.get("name")))
    def _ref(self, q): return next(iter(self.find(q)), None)
    def find_one(self, q, *a, **k):
        READS.append((self.name, q))
        h = self._ref(q); return dict(h) if h else None
    def find(self, q=None, *a, **k):
        q = q or {}
        return [d for d in self.docs
                if all(d.get(k2) == v for k2, v in q.items() if not isinstance(v, dict))]
    def update_one(self, q, ops, upsert=False):
        h = self._ref(q)
        if h is None:
            if not upsert: return types.SimpleNamespace(matched_count=0)
            h = dict(q); self.docs.append(h)
        h.update(ops.get("$set", {}))
        for k in ops.get("$unset", {}): h.pop(k, None)
        for k, v in ops.get("$addToSet", {}).items(): h.setdefault(k, []).append(v)
        return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, FakeColl(n))


DB = FakeDB()
st = types.ModuleType("Database"); st.get_bot_database = lambda c: DB
sys.modules["Database"] = st
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = object
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import GuildConfig

GUILD = 77


async def main():
    bot = types.SimpleNamespace(MongoClient=object())
    DB["servers"].docs.append({
        "guild_id": GUILD,
        "medialog_enabled": True, "medialog_channel": 100,
        "pinglog_enabled": True, "pinglog_channel": 200,
        "modlog_channel": 300,
        "discovery_channel": 400, "discovery_message": 500,
    })

    print("=== one read serves every cog ===")
    READS.clear()
    GuildConfig._cache.clear()
    for _ in range(4):                      # as if four cogs each asked
        cfg = await GuildConfig.get(bot, GUILD)
    print(f"  4 lookups -> {len(READS)} database read(s)")
    assert len(READS) == 1, READS
    assert cfg["medialog_channel"] == 100
    print("  cached across cogs OK")

    print("\n=== unconfigured guilds are cached too ===")
    READS.clear()
    for _ in range(5):
        empty = await GuildConfig.get(bot, 999)
    print(f"  5 lookups on an unknown guild -> {len(READS)} read(s), returned {empty}")
    assert len(READS) == 1, READS
    assert empty == {}, empty
    print("  negative result cached, returns {} not None OK")

    print("\n=== writes invalidate for everyone ===")
    await GuildConfig.update(bot, GUILD, {"medialog_channel": 111})
    READS.clear()
    cfg = await GuildConfig.get(bot, GUILD)
    assert len(READS) == 1, "a write should force the next read to refetch"
    assert cfg["medialog_channel"] == 111, cfg
    print("  update() invalidated and the new value is visible OK")

    await GuildConfig.update(bot, GUILD, unset={"modlog_channel": ""})
    cfg = await GuildConfig.get(bot, GUILD)
    assert "modlog_channel" not in cfg, cfg
    print("  unset works OK")

    print("\n=== a database blip serves stale rather than 'unconfigured' ===")
    cfg_before = await GuildConfig.get(bot, GUILD)
    orig = FakeColl.find_one
    def boom(self, q, *a, **k): raise RuntimeError("connection reset")
    FakeColl.find_one = boom
    GuildConfig._cache[GUILD] = (cfg_before, 0)     # force it to look expired
    got = await GuildConfig.get(bot, GUILD)
    FakeColl.find_one = orig
    assert got == cfg_before, "should have served the stale copy"
    print("  served the last known settings instead of switching features off OK")

    got = await GuildConfig.get(bot, 12345)          # never seen, and now DB works again
    assert got == {}, got

    print("\n=== pruning ===")
    GuildConfig._cache.clear()
    await GuildConfig.get(bot, GUILD)
    # Measured back from now, not 0. time.monotonic() counts from boot on Windows, so a
    # literal 0 is only "ancient" once the machine has been up longer than TTL * 4. This
    # passed for weeks and then failed the first time the suite ran on a freshly booted
    # machine, which is the worst way for a test to be wrong.
    GuildConfig._cache[GUILD] = (cfg_before, time.monotonic() - GuildConfig.TTL * 10)
    GuildConfig.prune()
    assert GUILD not in GuildConfig._cache, "stale entry should have been pruned"
    print("  prune() drops entries nobody has touched OK")

    print("\n=== indexes ===")
    INDEXES.clear()
    await GuildConfig.ensure_indexes(bot)
    for coll, keys, name in INDEXES:
        print(f"  {coll:12} {keys}  ({name})")
    covered = {c for c, _, _ in INDEXES}
    # departures is gone: it was write-only and the memberships collection replaced it, with
    # its own indexes owned by the Members cog.
    assert covered == {"servers", "roles"}, covered
    assert any(c == "servers" and keys == [("guild_id", 1)] for c, keys, _ in INDEXES)
    assert any(c == "roles" and keys == [("date", 1), ("guild_id", 1)] for c, keys, _ in INDEXES)
    print("  all three original collections indexed OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
