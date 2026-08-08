"""Joining a server, and clearing up after leaving one.

The deletion is the part that has to be right in both directions. Data must actually go, or it
sits in Mongo forever after somebody kicks the bot. And it must not go early, because a bot
that wipes a server's whole history the instant it is removed turns an accidental kick into a
disaster.

The last check here is the one that matters most over time: it scans the source for every
collection the bot touches and fails if a new one appears that nobody has said how to clean up.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")

import asyncio, datetime, re, sys, types
sys.path.insert(0, SRC_DIR)


class FakeColl:
    def __init__(self, name): self.name = name; self.docs = []
    def create_index(self, *a, **k): pass
    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict):
                if "$lte" in v and not (d.get(k) is not None and d[k] <= v["$lte"]):
                    return False
            elif d.get(k) != v:
                return False
        return True
    def find(self, q=None, *a, **k):
        return _Cursor([dict(d) for d in self.docs if self._match(d, q or {})])
    def find_one(self, q, *a, **k):
        hit = next((d for d in self.docs if self._match(d, q)), None)
        return dict(hit) if hit else None
    def update_one(self, q, ops, upsert=False):
        hit = next((d for d in self.docs if self._match(d, q)), None)
        if hit is None:
            if not upsert:
                return types.SimpleNamespace(matched_count=0)
            hit = dict(q); self.docs.append(hit)
        hit.update(ops.get("$set", {}))
        return types.SimpleNamespace(matched_count=1)
    def delete_one(self, q):
        hit = next((d for d in self.docs if self._match(d, q)), None)
        if hit is not None:
            self.docs.remove(hit)
        return types.SimpleNamespace(deleted_count=1 if hit else 0)
    def delete_many(self, q):
        gone = [d for d in self.docs if self._match(d, q)]
        for d in gone:
            self.docs.remove(d)
        return types.SimpleNamespace(deleted_count=len(gone))


class _Cursor(list):
    def limit(self, n): return _Cursor(self[:n])
    def sort(self, *a, **k): return self


class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, FakeColl(n))


DB = FakeDB()
st = types.ModuleType("Database"); st.get_bot_database = lambda c: DB
sys.modules["Database"] = st
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = lambda *a, **k: object()
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import discord
from discord.ext import commands

GUILD, OTHER_GUILD = 1, 2


class FakeChannel:
    def __init__(self, name, can_post=True):
        self.name = name; self.can_post = can_post; self.sent = []
    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=True, send_messages=self.can_post,
                                     embed_links=self.can_post)
    async def send(self, **kw): self.sent.append(kw)


class FakeOwner:
    def __init__(self, accepts=True): self.accepts = accepts; self.dms = []
    async def send(self, **kw):
        if not self.accepts:
            raise discord.Forbidden(
                types.SimpleNamespace(status=403, reason="Forbidden"), "closed")
        self.dms.append(kw)


def make_guild(system=None, channels=(), owner=None, gid=GUILD):
    g = types.SimpleNamespace(id=gid, name="Cool Server")
    g.me = object()
    g.system_channel = system
    g.text_channels = list(channels)
    g.owner = owner
    return g


def seed(collection, count, guild_id=GUILD, key="guild_id"):
    for i in range(count):
        DB[collection].docs.append({key: guild_id, "n": i})


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=42, name="Newt", avatar=None)
    bot.MongoClient = object()
    bot.get_guild = lambda gid: None
    await bot.load_extension("Cogs.Lifecycle")
    cog = bot.get_cog("Lifecycle")
    cog.sweep.cancel()          # the loop would just wait on a gateway that never opens
    import Cogs.Lifecycle as L

    print("=== it introduces itself in the server's system channel ===")
    system, general = FakeChannel("welcome"), FakeChannel("general")
    g = make_guild(system=system, channels=[general, system])
    await cog.on_guild_join(g)
    assert len(system.sent) == 1 and not general.sent, "the system channel is the right one"
    embed = system.sent[0]["embed"]
    body = "\n".join(f.value for f in embed.fields)
    for expected in ("/setchannel", "/logging setup", "/help"):
        assert expected in body, expected
    print(f"  posted in #welcome naming {len(embed.fields)} things to try OK")

    print("\n=== it falls back to somewhere it can actually post ===")
    locked, open_one = FakeChannel("rules", can_post=False), FakeChannel("chat")
    g = make_guild(system=locked, channels=[locked, open_one])
    await cog.on_guild_join(g)
    assert not locked.sent and len(open_one.sent) == 1, "should skip the one it can't post in"
    print("  system channel was locked, so it used #chat OK")

    print("\n=== and DMs the owner when there is nowhere at all ===")
    owner = FakeOwner()
    g = make_guild(system=None, channels=[FakeChannel("x", can_post=False)], owner=owner)
    await cog.on_guild_join(g)
    assert len(owner.dms) == 1, owner.dms
    assert "couldn't find a channel" in owner.dms[0]["content"]
    print("  owner got it by direct message OK")

    g = make_guild(system=None, channels=[], owner=FakeOwner(accepts=False))
    await cog.on_guild_join(g)          # must not raise when their DMs are closed
    g = make_guild(system=None, channels=[], owner=None)
    await cog.on_guild_join(g)          # nor when the owner isn't cached
    print("  closed DMs and a missing owner both handled OK")

    print("\n=== the dashboard link only appears when there is one ===")
    import os
    g = make_guild(system=FakeChannel("a"), channels=[])
    await cog.on_guild_join(g)
    assert not any("dashboard" in f.name.lower() for f in g.system_channel.sent[0]["embed"].fields)
    os.environ["DASHBOARD_URL"] = "https://newt.example/"
    g2 = make_guild(system=FakeChannel("a"), channels=[])
    await cog.on_guild_join(g2)
    fields = g2.system_channel.sent[0]["embed"].fields
    link = next(f for f in fields if "dashboard" in f.name.lower())
    assert "https://newt.example" in link.value and "example//" not in link.value
    os.environ.pop("DASHBOARD_URL")
    print("  absent when unset, trailing slash trimmed when set OK")

    print("\n=== leaving schedules deletion rather than doing it ===")
    DB.c.clear()
    for name in L.BY_GUILD_ID:
        seed(name, 3)
    await cog.on_guild_remove(make_guild())
    assert len(DB["departed_guilds"].docs) == 1
    assert DB["departed_guilds"].docs[0]["_id"] == GUILD
    for name in L.BY_GUILD_ID:
        assert len(DB[name].docs) == 3, f"{name} must not be touched yet"
    print(f"  noted as departed, all {len(L.BY_GUILD_ID)} collections untouched OK")

    print("\n=== nothing goes before the grace period is up ===")
    await cog.sweep()
    assert len(DB["departed_guilds"].docs) == 1
    assert len(DB["servers"].docs) == 3
    print(f"  swept on day 0 of {L.GRACE_DAYS}, nothing deleted OK")

    print("\n=== coming back cancels it ===")
    await cog.on_guild_join(make_guild(system=FakeChannel("a")))
    assert DB["departed_guilds"].docs == [], "the departure note should be torn up"
    assert len(DB["servers"].docs) == 3, "and the settings should still be there"
    print("  re-added inside the grace period, settings intact OK")

    print("\n=== once it expires, everything goes ===")
    DB.c.clear()
    for name in L.BY_GUILD_ID:
        seed(name, 4)
        seed(name, 2, guild_id=OTHER_GUILD)          # a different server, must survive
    DB["config_dirty"].docs.append({"_id": GUILD})
    DB["config_dirty"].docs.append({"_id": OTHER_GUILD})
    DB["counters"].docs.append({"_id": f"case:{GUILD}", "seq": 9})
    DB["counters"].docs.append({"_id": f"case:{OTHER_GUILD}", "seq": 4})

    stale = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=L.GRACE_DAYS + 1))
    DB["departed_guilds"].docs.append({"_id": GUILD, "at": stale, "name": "Cool Server"})
    await cog.sweep()

    for name in L.BY_GUILD_ID:
        left = [d for d in DB[name].docs]
        assert all(d["guild_id"] == OTHER_GUILD for d in left), f"{name} still has our data"
        assert len(left) == 2, f"{name} lost the other server's data"
    assert [d["_id"] for d in DB["config_dirty"].docs] == [OTHER_GUILD]
    assert [d["_id"] for d in DB["counters"].docs] == [f"case:{OTHER_GUILD}"]
    assert DB["departed_guilds"].docs == [], "the note should go with the data"
    print(f"  {len(L.BY_GUILD_ID)} collections plus config_dirty and the case counter cleared, "
          f"the other server untouched OK")

    print("\n=== a stale note for a server it is back in is ignored ===")
    DB.c.clear()
    seed("servers", 5)
    DB["departed_guilds"].docs.append({"_id": GUILD, "at": stale})
    bot.get_guild = lambda gid: object() if gid == GUILD else None
    await cog.sweep()
    assert len(DB["servers"].docs) == 5, "deleting a live server's settings is the worst case"
    assert DB["departed_guilds"].docs == [], "and the stale note should be cleared"
    bot.get_guild = lambda gid: None
    print("  settings kept, note cleared OK")

    print("\n=== forget reports what it removed ===")
    DB.c.clear()
    seed("servers", 2)
    seed("ratings", 7)
    removed = await cog.forget(GUILD)
    assert removed["servers"] == 2 and removed["ratings"] == 7, removed
    assert set(removed) >= set(L.BY_GUILD_ID), set(L.BY_GUILD_ID) - set(removed)
    print(f"  {removed['servers']} servers, {removed['ratings']} ratings, "
          f"{len(removed)} collections reported OK")

    print("\n=== every collection the bot uses is accounted for ===")
    # The check that matters in a year. Add a collection and forget to say how it is cleaned
    # up, and this fails rather than quietly leaking that data forever.
    known = set(L.BY_GUILD_ID) | set(L.BY_ID) | {
        "counters",           # keyed case:<guild_id>, handled separately in forget()
        "departed_guilds",    # the bookkeeping for this cog, deleted alongside
        "runtime",            # one global document, not per guild
        # Support tickets belong to the person who opened them, not to a server. Removing the
        # bot from one server is not a reason to erase somebody's support history, and a
        # ticket can be about no server at all.
        "tickets",
    }
    found = set()
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "web").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:_db_?|database|db\(\)|get_bot_database\([^)]*\))'
                             r'\[\s*["\']([a-z_]+)["\']\s*\]', text):
            found.add(m.group(1))

    assert found, "the scan found no collections at all, so it isn't checking anything"
    missing = found - known
    assert not missing, (
        f"these collections are used but not classified in Cogs/Lifecycle.py: {sorted(missing)}. "
        f"Add each to BY_GUILD_ID or BY_ID, or to the exemptions in this test.")
    print(f"  scanned the source, found {len(found)} collections, all accounted for OK")
    print(f"  ({', '.join(sorted(found))})")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
