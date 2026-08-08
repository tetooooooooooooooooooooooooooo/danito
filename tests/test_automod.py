"""Automod: what it catches, and more importantly what it leaves alone.

The failure that matters is not a missed spammer, it is a deleted message that should have
stayed. So most of this is about the things that must never trigger: a moderator, an exempt
role or channel, a word that merely contains a banned one, an allowed domain, somebody the bot
could not action by hand anyway.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")

import asyncio, sys, types
sys.path.insert(0, SRC_DIR)
sys.path.insert(0, WEB_DIR)


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
        return types.SimpleNamespace(matched_count=1)
    def insert_one(self, doc):
        self.docs.append(doc)
        return types.SimpleNamespace(inserted_id=len(self.docs))
    def find_one_and_update(self, q, ops, upsert=False, return_document=None):
        # Backs the case counter, which is how a case gets its number.
        h = self._ref(q)
        if h is None:
            h = dict(q); self.docs.append(h)
        for field, by in ops.get("$inc", {}).items():
            h[field] = h.get(field, 0) + by
        return dict(h)


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

GUILD, CHAN, QUIET = 1, 10, 11
BOT_ID, OWNER_ID = 42, 99


class FakeRole:
    def __init__(self, rid, position=1):
        self.id = rid; self.position = position; self.name = f"role{rid}"
        self.mention = f"<@&{rid}>"
    def __ge__(self, o): return self.position >= o.position
    def __eq__(self, o): return isinstance(o, FakeRole) and o.id == self.id
    def __hash__(self): return hash(self.id)


BOT_TOP = FakeRole(90, 50)
PLAIN = FakeRole(20, 5)
STAFFY = FakeRole(21, 6)


class FakeChannel:
    def __init__(self, cid=CHAN, staff=False):
        self.id = cid; self.name = "general"; self.mention = f"<#{cid}>"
        self.sent = []; self.staff = staff
    def permissions_for(self, m):
        allow = getattr(m, "is_staff", False)
        return types.SimpleNamespace(manage_messages=allow, manage_guild=allow,
                                     administrator=False)
    async def send(self, content=None, **kw):
        self.sent.append(content)


class FakeMember:
    def __init__(self, uid=500, roles=(PLAIN,), staff=False):
        self.id = uid; self.bot = False; self.roles = list(roles)
        self.guild = GUILD_OBJ
        self.is_staff = staff
        self.mention = f"<@{uid}>"
        self.timeouts = []
    @property
    def top_role(self): return max(self.roles, key=lambda r: r.position)
    def __str__(self): return f"user{self.id}"
    async def timeout(self, until, reason=None): self.timeouts.append((until, reason))
    async def kick(self, reason=None): self.guild.kicked.append(self.id)


class FakeMessage:
    def __init__(self, content="hi", author=None, channel=None, mentions=(), roles=()):
        self.guild = GUILD_OBJ
        self.author = author or FakeMember()
        self.channel = channel or CHANNELS[CHAN]
        self.content = content
        self.mentions = list(mentions)
        self.role_mentions = list(roles)
        self.webhook_id = None
        self.deleted = False
    async def delete(self): self.deleted = True


CHANNELS = {}
GUILD_OBJ = None


def reset():
    CHANNELS.clear()
    CHANNELS[CHAN] = FakeChannel(CHAN)
    CHANNELS[QUIET] = FakeChannel(QUIET)


def make_guild(can_timeout=True, can_kick=True, can_ban=True):
    g = types.SimpleNamespace(id=GUILD, name="Cool Server", owner_id=OWNER_ID)
    g.kicked, g.banned = [], []
    async def ban(member, reason=None): g.banned.append(member.id)
    g.ban = ban
    g.me = types.SimpleNamespace(
        id=BOT_ID, top_role=BOT_TOP,
        guild_permissions=types.SimpleNamespace(moderate_members=can_timeout,
                                                kick_members=can_kick,
                                                ban_members=can_ban,
                                                manage_messages=True))
    g.get_channel = lambda i: CHANNELS.get(i)
    g.get_role = lambda i: None
    return g


def automod(**kw):
    """Write an automod document, defaults off, with the named rules switched on."""
    rules = kw.pop("rules", {})
    doc = {"enabled": True, "notify": True, "exempt_staff": True,
           "exempt_roles": [], "exempt_channels": [], "timeout_minutes": 10,
           "rules": rules}
    doc.update(kw)
    DB["servers"].docs.clear()
    DB["servers"].docs.append({"guild_id": GUILD, "automod": doc})
    GuildConfig._cache.clear()


async def main():
    global GUILD_OBJ
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=BOT_ID, name="Newt", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.AutoMod")
    cog = bot.get_cog("AutoMod")
    cog.prune.cancel()
    import Cogs.AutoMod as A
    import store

    GUILD_OBJ = make_guild()
    reset()

    print("=== the bot's rules and the dashboard's rules agree ===")
    assert A.RULE_KEYS == store.AUTOMOD_RULE_KEYS, (A.RULE_KEYS, store.AUTOMOD_RULE_KEYS)
    assert set(A.DEFAULTS) == set(A.RULE_KEYS)
    assert [a for a, _ in store.AUTOMOD_ACTIONS] == list(A.ACTIONS)
    print(f"  {len(A.RULE_KEYS)} rules and {len(A.ACTIONS)} actions, identical in both OK")

    async def send(content, **kw):
        m = FakeMessage(content, **kw)
        await cog.on_message(m)
        return m

    print("\n=== banned words match whole words only ===")
    automod(rules={"words": {"on": True, "action": "delete", "list": ["ass", "spam"]}})
    assert (await send("you ass")).deleted
    assert (await send("SPAM")).deleted, "case shouldn't matter"
    for safe in ("class", "passive", "assassin", "spammy"):
        assert not (await send(safe)).deleted, safe
    print("  caught 'ass' and 'SPAM', left class, passive, assassin and spammy alone OK")

    print("\n=== zero width characters don't walk past it ===")
    # Splitting a word with invisible characters is the oldest way around a filter, and it
    # reads identically to everyone else in the channel.
    assert (await send("s​p​a​m")).deleted, "invisible characters between letters"
    assert (await send("​spam​")).deleted, "invisible padding around it"
    assert not (await send("s p a m")).deleted, \
        "real spaces are a different message and shouldn't be joined up"
    print("  invisible characters stripped, real spaces left alone OK")

    print("\n=== invites ===")
    automod(rules={"invites": {"on": True, "action": "delete"}})
    for bad in ("join discord.gg/abc123", "https://discord.com/invite/xyz", "dsc.gg/thing"):
        assert (await send(bad)).deleted, bad
    assert not (await send("I like discord")).deleted
    print("  three invite shapes caught, the word 'discord' left alone OK")

    print("\n=== links, with an allow list ===")
    automod(rules={"links": {"on": True, "action": "delete",
                             "allow": ["youtube.com", "tenor.com"]}})
    assert (await send("look https://evil.example/x")).deleted
    assert not (await send("https://youtube.com/watch?v=1")).deleted
    assert not (await send("https://www.youtube.com/x")).deleted, "www should count"
    assert not (await send("https://music.youtube.com/x")).deleted, "subdomains should count"
    assert (await send("https://notyoutube.com/x")).deleted, "but not a lookalike domain"
    print("  allowed site, its www and its subdomains pass; a lookalike doesn't OK")

    print("\n=== mass mentions ===")
    automod(rules={"mentions": {"on": True, "action": "delete", "limit": 3}})
    who = [FakeMember(uid) for uid in range(600, 606)]
    assert (await send("hi", mentions=who)).deleted
    assert not (await send("hi", mentions=who[:3])).deleted, "at the limit is fine"
    dupe = [who[0]] * 6
    assert not (await send("hi", mentions=dupe)).deleted, \
        "mentioning one person six times is one person"
    print("  6 people caught, 3 allowed, the same person repeated not counted six times OK")

    print("\n=== shouting ===")
    automod(rules={"caps": {"on": True, "action": "delete",
                            "percent": 70, "min_length": 12}})
    assert (await send("WHY IS NOBODY LISTENING TO ME")).deleted
    assert not (await send("STOP")).deleted, "short messages are exempt"
    assert not (await send("This is a normal sentence about things")).deleted
    assert not (await send("WHY?!?!")).deleted, "punctuation is not shouting"
    print("  long shouting caught; short, normal and punctuation left alone OK")

    print("\n=== emoji and line spam ===")
    automod(rules={"emoji": {"on": True, "action": "delete", "limit": 4}})
    assert (await send("🎉🎉🎉🎉🎉🎉")).deleted
    assert (await send("<:a:1><:b:2><:c:3><:d:4><:e:5>")).deleted, "custom emoji count too"
    assert not (await send("nice 🎉")).deleted
    automod(rules={"newlines": {"on": True, "action": "delete", "limit": 4}})
    assert (await send("a\nb\nc\nd\ne\nf")).deleted
    assert not (await send("a\nb")).deleted
    print("  emoji walls and long messages caught OK")

    print("\n=== flooding ===")
    automod(rules={"spam": {"on": True, "action": "delete", "count": 4, "seconds": 30}})
    cog._recent.clear()
    hits = [await send(f"message {i}") for i in range(5)]
    assert not any(m.deleted for m in hits[:3]), "the first few are fine"
    assert hits[3].deleted, "the fourth inside the window is a flood"
    print("  three allowed, the fourth caught OK")

    print("\n=== repeating yourself ===")
    automod(rules={"duplicates": {"on": True, "action": "delete", "count": 3}})
    cog._recent.clear()
    same = [await send("buy my thing") for _ in range(3)]
    assert not same[0].deleted and not same[1].deleted
    assert same[2].deleted
    cog._recent.clear()
    varied = [await send(t) for t in ("one", "two", "three", "four")]
    assert not any(m.deleted for m in varied)
    print("  third repeat caught, four different messages left alone OK")

    print("\n=== who it never touches ===")
    automod(rules={"words": {"on": True, "action": "delete", "list": ["spam"]}})
    assert (await send("spam")).deleted, "the rule does work on an ordinary member"

    assert not (await send("spam", author=FakeMember(staff=True))).deleted
    print("  a moderator OK")

    automod(exempt_channels=[QUIET],
            rules={"words": {"on": True, "action": "delete", "list": ["spam"]}})
    assert not (await send("spam", channel=CHANNELS[QUIET])).deleted
    assert (await send("spam")).deleted, "other channels still filtered"
    print("  an exempt channel OK")

    automod(exempt_roles=[STAFFY.id],
            rules={"words": {"on": True, "action": "delete", "list": ["spam"]}})
    assert not (await send("spam", author=FakeMember(roles=[PLAIN, STAFFY]))).deleted
    print("  an exempt role OK")

    automod(rules={"words": {"on": True, "action": "delete", "list": ["spam"]}})
    assert not (await send("spam", author=FakeMember(uid=OWNER_ID))).deleted
    print("  the server owner OK")

    above = FakeMember(uid=700, roles=[FakeRole(95, 80)])
    assert not (await send("spam", author=above)).deleted
    print("  somebody above the bot in the role list OK")

    bot_author = FakeMember(uid=800)
    bot_author.bot = True
    assert not (await send("spam", author=bot_author)).deleted
    print("  another bot OK")

    print("\n=== switched off means nothing happens ===")
    automod(enabled=False,
            rules={"words": {"on": True, "action": "delete", "list": ["spam"]}})
    assert not (await send("spam")).deleted
    automod(rules={"words": {"on": False, "action": "delete", "list": ["spam"]}})
    assert not (await send("spam")).deleted
    DB["servers"].docs.clear(); GuildConfig._cache.clear()
    assert not (await send("spam")).deleted
    print("  master off, rule off and unconfigured all quiet OK")

    print("\n=== one rule per message ===")
    automod(rules={"words": {"on": True, "action": "delete", "list": ["spam"]},
                   "caps": {"on": True, "action": "delete", "percent": 50,
                            "min_length": 3}})
    m = await send("SPAM SPAM SPAM SPAM")
    assert m.deleted
    cases = DB["mod_cases"].docs
    assert len(cases) == 0, "delete alone shouldn't record a case"
    print("  matched both, acted once OK")

    print("\n=== warn and timeout record a case ===")
    await bot.load_extension("Cogs.Moderation")
    DB["mod_cases"].docs.clear()
    automod(rules={"words": {"on": True, "action": "warn", "list": ["spam"]}})
    member = FakeMember()
    await send("spam", author=member)
    assert len(DB["mod_cases"].docs) == 1, DB["mod_cases"].docs
    case = DB["mod_cases"].docs[0]
    assert case["action"] == "warn" and "AutoMod" in case["reason"], case
    assert case["mod_id"] == BOT_ID
    assert not member.timeouts
    print(f"  warn -> case #{case['case_id']} reason {case['reason']!r} OK")

    DB["mod_cases"].docs.clear()
    automod(timeout_minutes=25,
            rules={"words": {"on": True, "action": "timeout", "list": ["spam"]}})
    member = FakeMember()
    await send("spam", author=member)
    assert len(member.timeouts) == 1, member.timeouts
    assert DB["mod_cases"].docs[0]["action"] == "timeout"
    assert DB["mod_cases"].docs[0]["duration"] == 25 * 60
    print("  timeout -> member muted for 25 minutes and a case recorded OK")

    print("\n=== a timeout it can't carry out still deletes ===")
    GUILD_OBJ = make_guild(can_timeout=False)
    DB["mod_cases"].docs.clear()
    member = FakeMember()
    m = await send("spam", author=member)
    assert m.deleted and not member.timeouts
    assert DB["mod_cases"].docs[0]["action"] == "warn", \
        "without Moderate Members it should record what actually happened"
    GUILD_OBJ = make_guild()
    print("  message removed, recorded as a warning rather than a timeout OK")

    print("\n=== kick and ban ===")
    DB["mod_cases"].docs.clear()
    automod(rules={"words": {"on": True, "action": "kick", "list": ["spam"]}})
    member = FakeMember()
    m = await send("spam", author=member)
    assert m.deleted and GUILD_OBJ.kicked == [member.id], GUILD_OBJ.kicked
    assert DB["mod_cases"].docs[0]["action"] == "kick"
    print("  kick removed them and recorded a case OK")

    GUILD_OBJ.kicked.clear(); DB["mod_cases"].docs.clear()
    automod(rules={"words": {"on": True, "action": "ban", "list": ["spam"]}})
    member = FakeMember()
    await send("spam", author=member)
    assert GUILD_OBJ.banned == [member.id], GUILD_OBJ.banned
    assert DB["mod_cases"].docs[0]["action"] == "ban"
    print("  ban removed them and recorded a case OK")

    print("\n=== without the permission it falls back rather than doing nothing ===")
    GUILD_OBJ = make_guild(can_ban=False)
    GUILD_OBJ.banned.clear(); DB["mod_cases"].docs.clear()
    member = FakeMember()
    m = await send("spam", author=member)
    assert m.deleted and not GUILD_OBJ.banned
    assert len(member.timeouts) == 1, "a ban it can't place should still stop them"
    case = DB["mod_cases"].docs[0]
    assert case["action"] == "timeout", case
    assert "couldn't ban" in case["reason"], case["reason"]
    print(f"  no Ban Members -> timeout, case says {case['reason'][-46:].strip()!r}")

    GUILD_OBJ = make_guild(can_ban=False, can_timeout=False)
    DB["mod_cases"].docs.clear()
    member = FakeMember()
    m = await send("spam", author=member)
    assert m.deleted and not member.timeouts
    assert DB["mod_cases"].docs[0]["action"] == "warn"
    print("  no timeout either -> still deleted and written down as a warning OK")
    GUILD_OBJ = make_guild()

    print("\n=== the hourly brake on kicks and bans ===")
    cog._removals.clear()
    GUILD_OBJ.banned.clear(); DB["mod_cases"].docs.clear()
    automod(max_removals=3,
            rules={"words": {"on": True, "action": "ban", "list": ["spam"]}})
    people = [FakeMember(uid=1000 + i) for i in range(5)]
    for p in people:
        await send("spam", author=p)

    assert len(GUILD_OBJ.banned) == 3, GUILD_OBJ.banned
    assert [p.id for p in people[:3]] == GUILD_OBJ.banned
    # The two past the limit are still dealt with, just reversibly.
    assert all(p.timeouts for p in people[3:]), "past the limit should become a timeout"
    actions = [c["action"] for c in DB["mod_cases"].docs]
    assert actions == ["ban", "ban", "ban", "timeout", "timeout"], actions
    assert "paused" in DB["mod_cases"].docs[3]["reason"], DB["mod_cases"].docs[3]["reason"]
    print(f"  3 banned, then it stopped: {actions} OK")

    print("\n=== the brake is per server ===")
    cog._removals.clear()
    automod(max_removals=1,
            rules={"words": {"on": True, "action": "ban", "list": ["spam"]}})
    assert cog._removal_allowed(1, 1) and not cog._removal_allowed(1, 1)
    assert cog._removal_allowed(2, 1), "a different server has its own allowance"
    print("  one server hitting the limit doesn't stop another OK")

    print("\n=== the notice, and switching it off ===")
    reset()
    automod(rules={"words": {"on": True, "action": "delete", "list": ["spam"]}})
    await send("spam")
    assert CHANNELS[CHAN].sent and "removed automatically" in CHANNELS[CHAN].sent[0]
    reset()
    automod(notify=False,
            rules={"words": {"on": True, "action": "delete", "list": ["spam"]}})
    await send("spam")
    assert not CHANNELS[CHAN].sent
    print("  said why by default, silent when switched off OK")

    print("\n=== the dashboard form is cleaned ===")
    class Form(dict):
        def getlist(self, k): return self.get(k, [])
    form = Form({
        "automod_enabled": "on", "automod_notify": "on",
        "am_on_words": "on", "am_action_words": "timeout",
        "am_words_list": "Spam, SCAM\nfree nitro, spam,,  ",
        "am_on_mentions": "on", "am_action_mentions": "nonsense",
        "am_mentions_limit": "999",
        "am_spam_count": "notanumber", "am_spam_seconds": "-4",
        "am_links_allow": "YouTube.com , tenor.com",
        "automod_timeout": "999999",
        "automod_exempt_roles": ["20", "999", "abc"],
        "automod_exempt_channels": ["10", "555"],
    })
    out = store.clean_automod(form, {10, 11}, {20, 21}, {})
    assert out["enabled"] is True and out["exempt_staff"] is False
    assert out["rules"]["words"]["list"] == ["spam", "scam", "free nitro"], \
        out["rules"]["words"]["list"]
    assert out["rules"]["links"]["allow"] == ["youtube.com", "tenor.com"]
    assert out["rules"]["mentions"]["action"] == "delete", "an unknown action falls back"
    assert out["rules"]["mentions"]["limit"] == 50, "clamped to the advertised maximum"
    assert out["rules"]["spam"]["seconds"] == 1, "clamped to the minimum"
    assert out["timeout_minutes"] == store.AUTOMOD_TIMEOUT_RANGE[1]
    assert out["exempt_roles"] == [20] and out["exempt_channels"] == [10], out
    assert out["rules"]["caps"]["on"] is False, "a rule not in the form is off, not missing"
    assert set(out["rules"]) == set(store.AUTOMOD_RULE_KEYS)
    print("  deduplicated, lowercased, clamped, unknown ids and actions dropped OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
