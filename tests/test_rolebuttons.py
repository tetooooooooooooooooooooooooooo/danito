"""Role buttons: publishing, clicking, and the checks that stop a click doing more than it should.

The interesting cases are the ones that only show up later. A button id is client-visible and
client-supplied on the way back, so the role it names has to be checked against the panel
rather than trusted. And a panel pointed at a deleted channel must not be retried forever by
the publish loop.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, datetime, sys, types
sys.path.insert(0, SRC_DIR)

from bson import ObjectId


class FakeColl:
    def __init__(self, name): self.name = name; self.docs = []
    def create_index(self, *a, **k): pass
    def _match(self, d, q):
        for k, v in q.items():
            if k == "$or":
                if not any(self._match(d, sub) for sub in v):
                    return False
            elif isinstance(v, dict):
                continue
            elif d.get(k) != v:
                return False
        return True
    def _ref(self, q): return next((d for d in self.docs if self._match(d, q)), None)
    def find_one(self, q, *a, **k):
        h = self._ref(q); return dict(h) if h else None
    def find(self, q=None, *a, **k):
        return _Cursor([dict(d) for d in self.docs if self._match(d, q or {})])
    def count_documents(self, q): return len(list(self.find(q)))
    def insert_one(self, doc):
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return types.SimpleNamespace(inserted_id=doc["_id"])
    def update_one(self, q, ops, upsert=False):
        h = self._ref(q)
        if h is None:
            if not upsert: return types.SimpleNamespace(matched_count=0)
            h = dict(q); self.docs.append(h)
        h.update(ops.get("$set", {}))
        return types.SimpleNamespace(matched_count=1)
    def delete_one(self, q):
        h = self._ref(q)
        if h is not None: self.docs.remove(h)
        return types.SimpleNamespace(deleted_count=1 if h else 0)


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
import RoleTools

GUILD, CHAN = 1, 2


class FakeRole:
    def __init__(self, rid, name, position=1, managed=False, default=False):
        self.id = rid; self.name = name; self.position = position
        self.managed = managed; self._default = default
        self.mention = f"<@&{rid}>"
    def is_default(self): return self._default
    def __ge__(self, other): return self.position >= other.position
    def __lt__(self, other): return self.position < other.position
    def __eq__(self, other): return isinstance(other, FakeRole) and other.id == self.id
    def __hash__(self): return hash(self.id)


BOT_TOP = FakeRole(90, "Newt", position=50)
RED = FakeRole(10, "Red", position=5)
BLUE = FakeRole(11, "Blue", position=6)
SECRET = FakeRole(12, "Admin", position=80)      # never on any panel
ALL = {r.id: r for r in (RED, BLUE, SECRET, BOT_TOP)}


class FakeMessage:
    def __init__(self, mid, channel): self.id = mid; self.channel = channel
        # edits and deletes are recorded so the publish path can be checked
    def __repr__(self): return f"<msg {self.id}>"
    async def edit(self, **kw): self.channel.edits.append(kw)
    async def delete(self): self.channel.deleted.append(self.id)


class FakeChannel:
    def __init__(self, cid=CHAN, can_post=True, existing=None):
        self.id = cid; self.name = "roles"; self.mention = f"<#{cid}>"
        self.can_post = can_post
        self.sent = []; self.edits = []; self.deleted = []
        self._existing = existing
        self._next_id = 900
    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=True, send_messages=self.can_post,
                                     embed_links=self.can_post)
    async def send(self, **kw):
        if not self.can_post: raise discord.Forbidden(types.SimpleNamespace(status=403), "no")
        self.sent.append(kw)
        self._next_id += 1
        return FakeMessage(self._next_id, self)
    async def fetch_message(self, mid):
        if self._existing is None:
            raise discord.NotFound(types.SimpleNamespace(status=404), "gone")
        return self._existing


class FakeMember:
    def __init__(self, guild, roles=()):
        self.id = 500; self.bot = False; self.guild = guild; self.roles = list(roles)
        self.added = []; self.removed = []
    async def add_roles(self, *roles, reason=None):
        self.added.extend(roles); self.roles.extend(roles)
    async def remove_roles(self, *roles, reason=None):
        self.removed.extend(roles)
        self.roles = [r for r in self.roles if r not in roles]


def make_guild(channel=None, known=None):
    known = ALL if known is None else known
    g = types.SimpleNamespace(id=GUILD, name="Cool Server")
    g.me = types.SimpleNamespace(
        top_role=BOT_TOP,
        guild_permissions=types.SimpleNamespace(manage_roles=True))
    g.get_role = lambda i: known.get(i)
    g.get_channel = lambda i: channel if (channel and i == channel.id) else None
    return g


class Resp:
    def __init__(self): self.calls = []; self.deferred = False
    async def defer(self, **kw): self.deferred = True
    async def send_message(self, *a, **kw): self.calls.append((a, kw))


class Followup:
    def __init__(self): self.calls = []
    async def send(self, *a, **kw): self.calls.append((a, kw))


def click(guild, member, custom_id):
    i = types.SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id},
        guild=guild, user=member, response=Resp())
    i.followup = Followup()
    return i


def cmd_interaction(guild):
    i = types.SimpleNamespace(guild=guild, user=FakeMember(guild), response=Resp())
    i.followup = Followup()
    return i


def said(i):
    if i.followup.calls:
        args, kw = i.followup.calls[0]
    else:
        args, kw = i.response.calls[0]
    return args[0] if args else kw.get("content", "")


def make_panel(**kw):
    doc = {"_id": ObjectId(), "guild_id": GUILD, "channel_id": CHAN, "message_id": None,
           "title": "Pick your roles", "description": None, "color": 0x3DDC97,
           "mode": "toggle", "roles": [{"role_id": RED.id, "label": "Red", "emoji": None},
                                       {"role_id": BLUE.id, "label": "Blue", "emoji": None}],
           "needs_publish": False, "publish_error": None,
           "created_at": datetime.datetime.now(datetime.timezone.utc)}
    doc.update(kw)
    DB["role_panels"].docs.append(doc)
    return doc


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.MongoClient = object()
    await bot.load_extension("Cogs.RoleButtons")
    cog = bot.get_cog("RoleButtons")
    cog.publish_pending.cancel()          # the loop would just wait on a gateway that never opens

    print("=== the button ids survive a restart ===")
    DB["role_panels"].docs.clear()
    panel = make_panel()
    view = cog._view(panel)
    ids = [b.custom_id for b in view.children]
    assert ids == [f"rr:{panel['_id']}:{RED.id}", f"rr:{panel['_id']}:{BLUE.id}"], ids
    for cid in ids:
        assert cog.BUTTON_ID.match(cid) if hasattr(cog, "BUTTON_ID") else True
    import Cogs.RoleButtons as RB
    assert all(RB.BUTTON_ID.match(c) for c in ids), ids
    print(f"  {ids[0]} reads back OK")

    print("\n=== a button with no label still renders ===")
    odd = make_panel(roles=[{"role_id": RED.id, "label": "", "emoji": None}])
    b = cog._view(odd).children[0]
    assert b.label, "Discord rejects a button with neither label nor emoji"
    print(f"  fell back to {b.label!r} OK")

    print("\n=== publishing posts once, then edits ===")
    DB["role_panels"].docs.clear()
    panel = make_panel(needs_publish=True)
    ch = FakeChannel()
    bot.get_guild = lambda gid: make_guild(channel=ch)
    await cog._publish(dict(panel))
    assert len(ch.sent) == 1, ch.sent
    stored = DB["role_panels"].docs[0]
    assert stored["message_id"] == 901 and stored["needs_publish"] is False
    assert stored["publish_error"] is None
    print(f"  posted, message id {stored['message_id']} saved OK")

    existing = FakeMessage(901, ch)
    ch._existing = existing
    stored["needs_publish"] = True
    await cog._publish(dict(stored))
    assert len(ch.sent) == 1, "a second post would leave two panels in the channel"
    assert len(ch.edits) == 1, ch.edits
    print("  second publish edited the same message OK")

    print("\n=== a panel that can't be posted records why and stops trying ===")
    DB["role_panels"].docs.clear()
    panel = make_panel(needs_publish=True)
    bot.get_guild = lambda gid: make_guild(channel=None)
    await cog._publish(dict(panel))
    stored = DB["role_panels"].docs[0]
    assert stored["needs_publish"] is False, "retrying forever would hammer the API"
    assert "channel is gone" in stored["publish_error"], stored["publish_error"]
    print(f"  {stored['publish_error']}")

    DB["role_panels"].docs.clear()
    panel = make_panel(needs_publish=True)
    bot.get_guild = lambda gid: make_guild(channel=FakeChannel(can_post=False))
    await cog._publish(dict(panel))
    stored = DB["role_panels"].docs[0]
    assert "missing" in stored["publish_error"].lower(), stored["publish_error"]
    assert stored["needs_publish"] is False
    print(f"  {stored['publish_error']}")

    DB["role_panels"].docs.clear()
    panel = make_panel(needs_publish=True, roles=[])
    ch = FakeChannel()
    bot.get_guild = lambda gid: make_guild(channel=ch)
    await cog._publish(dict(panel))
    assert not ch.sent, "an empty panel has nothing to show"
    print("  an empty panel is not posted OK")

    print("\n=== deleting takes the message with it ===")
    DB["role_panels"].docs.clear()
    ch = FakeChannel()
    ch._existing = FakeMessage(901, ch)
    panel = make_panel(message_id=901, pending_delete=True)
    bot.get_guild = lambda gid: make_guild(channel=ch)
    await cog._destroy(dict(panel))
    assert ch.deleted == [901], ch.deleted
    assert DB["role_panels"].docs == [], "the record should go too"
    print("  message deleted and record dropped OK")

    print("\n=== clicking toggles ===")
    DB["role_panels"].docs.clear()
    panel = make_panel()
    g = make_guild(); m = FakeMember(g)
    i = click(g, m, f"rr:{panel['_id']}:{RED.id}")
    await cog.on_interaction(i)
    assert m.added == [RED], m.added
    assert "now have **Red**" in said(i), said(i)
    print(f"  {said(i)}")

    i = click(g, m, f"rr:{panel['_id']}:{RED.id}")
    await cog.on_interaction(i)
    assert m.removed == [RED], m.removed
    assert "back off you" in said(i)
    print(f"  {said(i)}")

    print("\n=== one role only swaps instead of stacking ===")
    DB["role_panels"].docs.clear()
    panel = make_panel(mode="single")
    g = make_guild(); m = FakeMember(g, roles=[RED])
    i = click(g, m, f"rr:{panel['_id']}:{BLUE.id}")
    await cog.on_interaction(i)
    assert m.removed == [RED], m.removed
    assert m.added == [BLUE], m.added
    print("  Red came off when Blue went on OK")

    print("\n=== a crafted id can't grant a role that isn't on the panel ===")
    DB["role_panels"].docs.clear()
    panel = make_panel()
    g = make_guild(); m = FakeMember(g)
    i = click(g, m, f"rr:{panel['_id']}:{SECRET.id}")
    await cog.on_interaction(i)
    assert m.added == [], "Admin is not on this panel and must not be handed out"
    assert "out of date" in said(i), said(i)
    print(f"  asked for Admin, got: {said(i)[:64]}")

    print("\n=== other buttons are left alone ===")
    for foreign in ("rating:7", "", "rr:nothex:10", f"rr:{panel['_id']}:notanumber"):
        i = click(make_guild(), FakeMember(make_guild()), foreign)
        await cog.on_interaction(i)
        assert not i.response.deferred and not i.followup.calls, foreign
    print("  the ratings survey and malformed ids pass straight through OK")

    print("\n=== a deleted panel says so rather than failing silently ===")
    DB["role_panels"].docs.clear()
    g = make_guild(); m = FakeMember(g)
    i = click(g, m, f"rr:{ObjectId()}:{RED.id}")
    await cog.on_interaction(i)
    assert "deleted" in said(i), said(i)
    print(f"  {said(i)[:70]}")

    print("\n=== a panel from another server is not reachable ===")
    DB["role_panels"].docs.clear()
    panel = make_panel(guild_id=999)
    g = make_guild(); m = FakeMember(g)
    i = click(g, m, f"rr:{panel['_id']}:{RED.id}")
    await cog.on_interaction(i)
    assert m.added == []
    print("  cross-server click refused OK")

    print("\n=== /rolepanel create then addrole ===")
    DB["role_panels"].docs.clear()
    ch = FakeChannel()
    g = make_guild(channel=ch)
    i = cmd_interaction(g)
    await cog.create.callback(cog, i, channel=ch, title="Colours", description=None, mode=None)
    assert len(DB["role_panels"].docs) == 1
    made = DB["role_panels"].docs[0]
    assert made["needs_publish"] is False, "nothing to post until it has a role"
    print("  created, not yet posted OK")

    i = cmd_interaction(g)
    await cog.addrole.callback(cog, i, panel=str(made["_id"]), role=RED, label=None, emoji=None)
    made = DB["role_panels"].docs[0]
    assert made["roles"] == [{"role_id": RED.id, "label": "Red", "emoji": None}], made["roles"]
    assert made["needs_publish"] is True
    print("  role added, queued for posting, label defaulted to the role name OK")

    i = cmd_interaction(g)
    await cog.addrole.callback(cog, i, panel=str(made["_id"]), role=SECRET, label=None, emoji=None)
    assert "above my own" in said(i), said(i)
    assert len(DB["role_panels"].docs[0]["roles"]) == 1
    print(f"  {said(i)[:66]}")

    i = cmd_interaction(g)
    await cog.addrole.callback(cog, i, panel=str(made["_id"]), role=RED, label=None, emoji=None)
    assert "already on that panel" in said(i)
    print("  duplicate refused OK")

    print("\n=== /rolepanel removerole ===")
    i = cmd_interaction(g)
    await cog.removerole.callback(cog, i, panel=str(made["_id"]), role=RED)
    made = DB["role_panels"].docs[0]
    assert made["roles"] == [] and made["needs_publish"] is False
    print("  last role off, so nothing is queued OK")

    print("\n=== a panel id from elsewhere is not editable ===")
    DB["role_panels"].docs.clear()
    other = make_panel(guild_id=999)
    i = cmd_interaction(make_guild())
    await cog.addrole.callback(cog, i, panel=str(other["_id"]), role=RED, label=None, emoji=None)
    assert "couldn't find that panel" in said(i).lower(), said(i)
    assert len(DB["role_panels"].docs[0]["roles"]) == 2, "the other server's panel is untouched"
    print("  refused OK")

    i = cmd_interaction(make_guild())
    await cog.addrole.callback(cog, i, panel="not-an-objectid", role=RED, label=None, emoji=None)
    assert "couldn't find that panel" in said(i).lower()
    print("  a malformed id is handled, not raised OK")

    print("\n=== emoji parsing never takes a panel down ===")
    for raw in (None, "", "🎉", "<:custom:123456789012345678>", "not an emoji at all", "::::"):
        RoleTools.parse_emoji(raw)
    print("  every input returned rather than raised OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
