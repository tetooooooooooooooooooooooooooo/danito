"""Autorole: the join path, the rules screen path, and the commands.

The membership screening case is the one that matters. A member arrives `pending` when the
server has a rules gate, and roles handed to a pending member are discarded by Discord, so an
autorole that only listens to on_member_join looks fine in a test server with no rules screen
and does nothing on a real one.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, sys, types
sys.path.insert(0, SRC_DIR)


class FakeColl:
    def __init__(self, name): self.name = name; self.docs = []
    def create_index(self, *a, **k): pass
    def _ref(self, q): return next(iter(self.find(q)), None)
    def find_one(self, q, *a, **k):
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
        for field, value in ops.get("$addToSet", {}).items():
            h.setdefault(field, [])
            if value not in h[field]:
                h[field].append(value)
        for field, value in ops.get("$pull", {}).items():
            if field not in h:
                continue
            drop = set(value["$in"]) if isinstance(value, dict) else {value}
            h[field] = [v for v in h[field] if v not in drop]
        return types.SimpleNamespace(matched_count=1)


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
import GuildConfig
import RoleTools

GUILD = 1


class FakeRole:
    def __init__(self, rid, name, position=1, managed=False, default=False):
        self.id = rid; self.name = name; self.position = position
        self.managed = managed; self._default = default
        self.mention = f"<@&{rid}>"
    def is_default(self): return self._default
    # discord.Role orders by position, which is what the hierarchy rule turns on.
    def __ge__(self, other): return self.position >= other.position
    def __lt__(self, other): return self.position < other.position
    def __eq__(self, other): return isinstance(other, FakeRole) and other.id == self.id
    def __hash__(self): return hash(self.id)


BOT_TOP = FakeRole(90, "Newt", position=50)
NORMAL = FakeRole(10, "Member", position=5)
SECOND = FakeRole(11, "Reader", position=6)
TOO_HIGH = FakeRole(12, "Admin", position=80)
MANAGED = FakeRole(13, "Booster", position=4, managed=True)
EVERYONE = FakeRole(14, "@everyone", position=0, default=True)
ALL = {r.id: r for r in (NORMAL, SECOND, TOO_HIGH, MANAGED, EVERYONE, BOT_TOP)}


def make_guild(can_manage=True, known=None):
    known = ALL if known is None else known
    g = types.SimpleNamespace(id=GUILD, name="Cool Server")
    g.me = types.SimpleNamespace(
        top_role=BOT_TOP,
        guild_permissions=types.SimpleNamespace(manage_roles=can_manage))
    g.get_role = lambda i: known.get(i)
    return g


class FakeMember:
    def __init__(self, guild, pending=False, roles=()):
        self.id = 500; self.bot = False; self.guild = guild
        self.pending = pending; self.roles = list(roles)
        self.added = []
    async def add_roles(self, *roles, reason=None):
        self.added.extend(roles); self.roles.extend(roles)


class Resp:
    def __init__(self): self.calls = []
    async def send_message(self, *a, **kw): self.calls.append((a, kw))


def interaction(guild):
    return types.SimpleNamespace(guild=guild, user=FakeMember(guild), response=Resp())


def settings(**kw):
    DB["servers"].docs.clear()
    if kw:
        DB["servers"].docs.append({"guild_id": GUILD, **kw})
    GuildConfig._cache.clear()


def reply(i):
    args, kw = i.response.calls[0]
    return args[0] if args else kw.get("content", "")


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.MongoClient = object()
    await bot.load_extension("Cogs.AutoRole")
    cog = bot.get_cog("AutoRole")

    print("=== RoleTools says why, not just no ===")
    g = make_guild()
    assert RoleTools.why_not(g, NORMAL) is None
    for role, needle in ((TOO_HIGH, "above my own"), (MANAGED, "integration"),
                         (EVERYONE, "@everyone")):
        why = RoleTools.why_not(g, role)
        assert why and needle in why, (role.name, why)
        print(f"  {role.name:9} -> {why[:58]}")
    assert "Manage Roles" in RoleTools.why_not(make_guild(can_manage=False), NORMAL)
    print("  no permission at all is reported too OK")

    print("\n=== a plain join gets the roles ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id, SECOND.id])
    g = make_guild(); m = FakeMember(g)
    await cog.on_member_join(m)
    assert m.added == [NORMAL, SECOND], m.added
    print(f"  handed out {[r.name for r in m.added]} in one call OK")

    print("\n=== a member behind the rules screen waits ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id])
    g = make_guild(); m = FakeMember(g, pending=True)
    await cog.on_member_join(m)
    assert m.added == [], "roles given to a pending member are thrown away by Discord"
    print("  join while pending: nothing handed out OK")

    after = FakeMember(g, pending=False)
    await cog.on_member_update(m, after)
    assert after.added == [NORMAL], after.added
    print("  once they accept: role arrives OK")

    print("\n=== other member updates are ignored ===")
    quiet = FakeMember(g)
    await cog.on_member_update(FakeMember(g), quiet)     # neither is pending
    assert quiet.added == []
    print("  a nickname or status change does not re-run it OK")

    print("\n=== roles it can't hand out are skipped, not attempted ===")
    settings(autorole_enabled=True,
             autorole_ids=[NORMAL.id, TOO_HIGH.id, MANAGED.id])
    g = make_guild(); m = FakeMember(g)
    await cog.on_member_join(m)
    assert m.added == [NORMAL], [r.name for r in m.added]
    print("  gave Member, skipped Admin and Booster OK")

    print("\n=== a role already held is not handed out again ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id, SECOND.id])
    g = make_guild(); m = FakeMember(g, roles=[NORMAL])
    await cog.on_member_join(m)
    assert m.added == [SECOND], [r.name for r in m.added]
    print("  only the missing one OK")

    print("\n=== a deleted role removes itself from the settings ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id, 9999])
    g = make_guild(); m = FakeMember(g)
    await cog.on_member_join(m)
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["autorole_ids"] == [NORMAL.id], cfg["autorole_ids"]
    assert m.added == [NORMAL]
    print("  9999 dropped, so it can't fail on every future join OK")

    print("\n=== switched off means nothing happens ===")
    settings(autorole_enabled=False, autorole_ids=[NORMAL.id])
    g = make_guild(); m = FakeMember(g)
    await cog.on_member_join(m)
    assert m.added == []
    print("  disabled OK")

    settings()
    g = make_guild(); m = FakeMember(g)
    await cog.on_member_join(m)
    assert m.added == []
    print("  unconfigured server OK")

    print("\n=== bots never get autoroled ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id])
    g = make_guild(); m = FakeMember(g); m.bot = True
    await cog.on_member_join(m)
    assert m.added == []
    print("  a joining bot is left alone OK")

    print("\n=== /autorole add ===")
    settings()
    i = interaction(make_guild())
    await cog.add.callback(cog, i, role=NORMAL)
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["autorole_ids"] == [NORMAL.id] and cfg["autorole_enabled"] is True
    print(f"  {reply(i)[:64]}")

    i = interaction(make_guild())
    await cog.add.callback(cog, i, role=NORMAL)
    assert "already" in reply(i).lower()
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["autorole_ids"] == [NORMAL.id], "a duplicate must not be stored twice"
    print("  adding the same role twice is refused OK")

    i = interaction(make_guild())
    await cog.add.callback(cog, i, role=TOO_HIGH)
    said = reply(i)
    assert "above my own" in said, said
    cfg = await GuildConfig.get(bot, GUILD)
    assert TOO_HIGH.id not in cfg["autorole_ids"], "must not save a role it can't give"
    print(f"  {said[:70]}")

    print("\n=== the limit holds ===")
    filler = {}
    settings(autorole_enabled=True, autorole_ids=list(range(100, 110)))   # already 10
    extra = FakeRole(777, "Extra", position=3)
    i = interaction(make_guild(known={**ALL, 777: extra}))
    await cog.add.callback(cog, i, role=extra)
    assert "limit" in reply(i).lower(), reply(i)
    cfg = await GuildConfig.get(bot, GUILD)
    assert len(cfg["autorole_ids"]) == 10
    print(f"  {reply(i)[:70]}")

    print("\n=== /autorole remove ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id, SECOND.id])
    i = interaction(make_guild())
    await cog.remove.callback(cog, i, role=NORMAL)
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["autorole_ids"] == [SECOND.id], cfg["autorole_ids"]
    print("  removed OK")

    i = interaction(make_guild())
    await cog.remove.callback(cog, i, role=NORMAL)
    assert "wasn't on the list" in reply(i)
    print("  removing one that isn't there says so OK")

    print("\n=== /autorole list flags what is broken ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id, TOO_HIGH.id, 9999])
    i = interaction(make_guild())
    await cog.show.callback(cog, i)
    embed = i.response.calls[0][1]["embed"]
    body = "\n".join(f.value for f in embed.fields)
    assert "above my own" in body and "deleted" in body, body
    assert embed.footer.text, "a broken list should explain itself"
    print("  names the too-high role and the deleted one OK")

    settings()
    i = interaction(make_guild())
    await cog.show.callback(cog, i)
    assert "Nothing is handed out" in i.response.calls[0][1]["embed"].description
    print("  a fresh server gets pointed at /autorole add OK")

    print("\n=== /autorole on refuses when there's nothing to hand out ===")
    settings()
    i = interaction(make_guild())
    await cog.on.callback(cog, i)
    assert "no roles" in reply(i).lower(), reply(i)
    cfg = await GuildConfig.get(bot, GUILD)
    assert not cfg.get("autorole_enabled")
    print("  won't switch on an empty list OK")

    print("\n=== off keeps the list ===")
    settings(autorole_enabled=True, autorole_ids=[NORMAL.id])
    i = interaction(make_guild())
    await cog.off.callback(cog, i)
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["autorole_enabled"] is False and cfg["autorole_ids"] == [NORMAL.id]
    print("  disabled, list kept OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
