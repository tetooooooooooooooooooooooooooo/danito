"""The server log: where each event goes, and the cases where it must stay quiet.

The routing is the part worth testing hardest. An event with its own channel goes there, one
without falls back to the shared channel, and with neither it goes nowhere rather than
silently to the wrong place. The rest is the quiet cases: the loop guard, the link-unfurl
edit, and not reporting a deleted image twice when MediaLog already has the file.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, datetime, sys, types

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

GUILD, MAIN, OTHER, ROOM = 1, 900, 901, 902
BOT_ID = 42


class FakeChannel:
    def __init__(self, cid, name="room", can_post=True):
        self.id = cid; self.name = name; self.mention = f"<#{cid}>"
        self.can_post = can_post; self.sent = []
        self.category = None
    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=True, send_messages=self.can_post,
                                     embed_links=self.can_post)
    async def send(self, **kw):
        if not self.can_post:
            raise discord.Forbidden(types.SimpleNamespace(status=403), "no")
        self.sent.append(kw)


CHANNELS = {}


def reset_channels(main_ok=True):
    CHANNELS.clear()
    CHANNELS[MAIN] = FakeChannel(MAIN, "server-log", main_ok)
    CHANNELS[OTHER] = FakeChannel(OTHER, "join-log")
    CHANNELS[ROOM] = FakeChannel(ROOM, "general")


class FakeAudit:
    def __init__(self, entries): self.entries = entries
    def __call__(self, limit=None, action=None): return self
    def __aiter__(self):
        self._it = iter(self.entries); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration


def make_guild(audit=None, can_audit=True):
    g = types.SimpleNamespace(id=GUILD, name="Cool Server", member_count=100)
    g.me = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(
        view_audit_log=can_audit, manage_roles=True))
    g.get_channel = lambda i: CHANNELS.get(i)
    g.audit_logs = FakeAudit(audit or [])
    return g


class FakeUser:
    def __init__(self, uid=500, name="someone", bot=False):
        self.id = uid; self.bot = bot; self.mention = f"<@{uid}>"
        self.display_avatar = types.SimpleNamespace(url="https://e.com/a.png")
        self._n = name
        self.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
    def __str__(self): return self._n


class FakeMember(FakeUser):
    def __init__(self, guild, uid=500, nick=None, roles=()):
        super().__init__(uid)
        self.guild = guild; self.nick = nick; self.roles = list(roles)
        self.joined_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)


class FakeRole:
    def __init__(self, rid, name, default=False):
        self.id = rid; self.name = name; self.mention = f"<@&{rid}>"; self._d = default
    def is_default(self): return self._d
    def __eq__(self, o): return isinstance(o, FakeRole) and o.id == self.id
    def __hash__(self): return hash(self.id)


class FakeMessage:
    def __init__(self, guild, channel, author, content="hello", attachments=()):
        self.guild = guild; self.channel = channel; self.author = author
        self.content = content; self.attachments = list(attachments)
        self.jump_url = "https://discord.com/x"


def settings(**kw):
    DB["servers"].docs.clear()
    if kw:
        DB["servers"].docs.append({"guild_id": GUILD, **kw})
    GuildConfig._cache.clear()


def all_events(on=True, **overrides):
    import Cogs.Logging as L
    out = {k: {"on": on, "channel": None} for k in L.EVENT_KEYS}
    for key, value in overrides.items():
        out[key] = value
    return out


def posted():
    return {cid: len(ch.sent) for cid, ch in CHANNELS.items() if ch.sent}


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=BOT_ID, name="Newt", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Logging")
    cog = bot.get_cog("Logging")
    import Cogs.Logging as L

    print("=== the bot's list and the dashboard's list agree ===")
    import store
    assert L.EVENT_KEYS == store.LOG_EVENT_KEYS, (L.EVENT_KEYS, store.LOG_EVENT_KEYS)
    print(f"  {len(L.EVENT_KEYS)} events, identical in both processes OK")

    print("\n=== everything to one channel ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    await cog.on_message_delete(FakeMessage(g, CHANNELS[ROOM], FakeUser(), "bye"))
    await cog.on_member_join(FakeMember(g))
    assert posted() == {MAIN: 2}, posted()
    print("  a delete and a join both landed in #server-log OK")

    print("\n=== one event sent somewhere of its own ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN,
             log_events=all_events(member_join={"on": True, "channel": OTHER}))
    g = make_guild()
    await cog.on_message_delete(FakeMessage(g, CHANNELS[ROOM], FakeUser()))
    await cog.on_member_join(FakeMember(g))
    assert posted() == {MAIN: 1, OTHER: 1}, posted()
    print("  the join went to #join-log, the delete stayed in #server-log OK")

    print("\n=== an event with its own channel works with no shared one ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=None,
             log_events=all_events(member_join={"on": True, "channel": OTHER}))
    g = make_guild()
    await cog.on_member_join(FakeMember(g))
    await cog.on_message_delete(FakeMessage(g, CHANNELS[ROOM], FakeUser()))
    assert posted() == {OTHER: 1}, posted()
    print("  the join logged, the delete had nowhere to go and was dropped OK")

    print("\n=== the off switches ===")
    for label, cfg in (
            ("logging switched off", dict(logging_enabled=False, log_channel=MAIN,
                                          log_events=all_events())),
            ("this event switched off", dict(logging_enabled=True, log_channel=MAIN,
                                             log_events=all_events(on=False))),
            ("nothing configured at all", {}),
    ):
        reset_channels()
        settings(**cfg)
        g = make_guild()
        await cog.on_message_delete(FakeMessage(g, CHANNELS[ROOM], FakeUser()))
        await cog.on_member_join(FakeMember(g))
        assert posted() == {}, (label, posted())
        print(f"  {label}: nothing sent OK")

    print("\n=== the log never logs itself ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    # somebody deletes a message inside the log channel
    await cog.on_message_delete(FakeMessage(g, CHANNELS[MAIN], FakeUser()))
    assert posted() == {}, posted()
    print("  a delete inside #server-log produced no entry OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN,
             log_events=all_events(member_join={"on": True, "channel": OTHER}))
    g = make_guild()
    await cog.on_message_delete(FakeMessage(g, CHANNELS[OTHER], FakeUser()))
    assert posted() == {}, "a channel used by any event counts as a log channel"
    print("  and neither did one inside #join-log OK")

    print("\n=== the bot's own messages are never logged ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    await cog.on_message_delete(FakeMessage(g, CHANNELS[ROOM], FakeUser(BOT_ID, bot=True)))
    assert posted() == {}, posted()
    print("  no feedback loop OK")

    print("\n=== a link unfurling is not an edit ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    same = FakeMessage(g, CHANNELS[ROOM], FakeUser(), "look https://example.com")
    await cog.on_message_edit(same, FakeMessage(g, CHANNELS[ROOM], FakeUser(),
                                                "look https://example.com"))
    assert posted() == {}, "Discord fires an edit when it builds the preview"
    print("  unchanged text produced nothing OK")

    await cog.on_message_edit(same, FakeMessage(g, CHANNELS[ROOM], FakeUser(), "changed"))
    assert posted() == {MAIN: 1}, posted()
    embed = CHANNELS[MAIN].sent[0]["embed"]
    names = [f.name for f in embed.fields]
    assert names == ["Before", "After"], names
    print("  a real edit logged both versions OK")

    print("\n=== a deleted image is reported once, not twice ===")
    att = types.SimpleNamespace(filename="cat.png")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events(),
             medialog_enabled=True, medialog_channel=ROOM)
    g = make_guild()
    await cog.on_message_delete(FakeMessage(g, CHANNELS[ROOM], FakeUser(), "", [att]))
    assert posted() == {}, "MediaLog has the file, so it reports this one"
    print("  with the media log on, left to it OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    await cog.on_message_delete(FakeMessage(g, CHANNELS[ROOM], FakeUser(), "", [att]))
    assert posted() == {MAIN: 1}, posted()
    body = CHANNELS[MAIN].sent[0]["embed"]
    assert any("cat.png" in (f.value or "") for f in body.fields)
    print("  with it off, logged here with the filename OK")

    print("\n=== bans name who did it when the audit log is readable ===")
    entry = types.SimpleNamespace(
        created_at=datetime.datetime.now(datetime.timezone.utc),
        target=types.SimpleNamespace(id=500),
        user=FakeUser(7, "a mod"), reason="spam")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild(audit=[entry])
    await cog.on_member_ban(g, FakeUser(500))
    fields = {f.name: f.value for f in CHANNELS[MAIN].sent[0]["embed"].fields}
    assert fields.get("Reason") == "spam", fields
    assert "<@7>" in fields.get("By", ""), fields
    print(f"  by {fields['By']}, reason {fields['Reason']!r} OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild(can_audit=False)
    await cog.on_member_ban(g, FakeUser(500))
    assert len(CHANNELS[MAIN].sent) == 1, "the ban itself must still log"
    assert not CHANNELS[MAIN].sent[0]["embed"].fields, "no audit access means no By field"
    print("  without View Audit Log it still logs the ban, just without who OK")

    print("\n=== a ban placed with /ban is not logged twice ===")
    # The bot is the one calling guild.ban, so the audit log names the bot.
    by_bot = types.SimpleNamespace(
        created_at=datetime.datetime.now(datetime.timezone.utc),
        target=types.SimpleNamespace(id=500),
        user=FakeUser(BOT_ID, "Newt"), reason="tet: spam")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events(),
             modlog_channel=OTHER)
    g = make_guild(audit=[by_bot])
    await cog.on_member_ban(g, FakeUser(500))
    assert posted() == {}, "the mod log already wrote this one as a numbered case"
    print("  with a mod log set up, left to it OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild(audit=[by_bot])
    await cog.on_member_ban(g, FakeUser(500))
    assert posted() == {MAIN: 1}, "with no mod log this is the only record there would be"
    print("  with no mod log, logged here instead OK")

    # Somebody banning through Discord's own menu has no case, so it must always log.
    by_hand = types.SimpleNamespace(
        created_at=datetime.datetime.now(datetime.timezone.utc),
        target=types.SimpleNamespace(id=500),
        user=FakeUser(7, "a mod"), reason=None)
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events(),
             modlog_channel=OTHER)
    g = make_guild(audit=[by_hand])
    await cog.on_member_ban(g, FakeUser(500))
    assert posted() == {MAIN: 1}, posted()
    print("  a ban placed through Discord itself still logged OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events(),
             modlog_channel=OTHER)
    g = make_guild(audit=[by_bot])
    await cog.on_member_unban(g, FakeUser(500))
    assert posted() == {}, "unbans follow the same rule"
    print("  unbans behave the same way OK")

    print("\n=== nicknames and roles are separate events ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN,
             log_events=all_events(member_roles={"on": False, "channel": None}))
    g = make_guild()
    red = FakeRole(10, "Red")
    before = FakeMember(g, nick="old", roles=[])
    after = FakeMember(g, nick="new", roles=[red])
    await cog.on_member_update(before, after)
    assert posted() == {MAIN: 1}, "only the nickname is switched on"
    assert CHANNELS[MAIN].sent[0]["embed"].title == "Nickname changed"
    print("  role changes off, nickname on: one entry OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    await cog.on_member_update(before, after)
    assert posted() == {MAIN: 2}, posted()
    titles = sorted(s["embed"].title for s in CHANNELS[MAIN].sent)
    assert titles == ["Nickname changed", "Roles changed"], titles
    print("  both on: two entries OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    quiet = FakeMember(g, nick="same", roles=[red])
    await cog.on_member_update(quiet, FakeMember(g, nick="same", roles=[red]))
    assert posted() == {}, "a status change is not a nickname or role change"
    print("  an unrelated member update produced nothing OK")

    print("\n=== voice: only actual movement ===")
    vc1 = types.SimpleNamespace(id=700, name="General")
    vc2 = types.SimpleNamespace(id=701, name="Music")

    def state(channel, mute=False):
        return types.SimpleNamespace(channel=channel, self_mute=mute)

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    who = FakeMember(g)
    await cog.on_voice_state_update(who, state(None), state(vc1))
    await cog.on_voice_state_update(who, state(vc1), state(vc2))
    await cog.on_voice_state_update(who, state(vc2), state(None))
    assert posted() == {MAIN: 3}, posted()
    titles = [s["embed"].title for s in CHANNELS[MAIN].sent]
    assert titles == ["Joined voice", "Moved voice channel", "Left voice"], titles
    assert "General" in CHANNELS[MAIN].sent[1]["embed"].description
    assert "Music" in CHANNELS[MAIN].sent[1]["embed"].description
    print(f"  {titles} OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    # Muting, deafening, streaming and cameras all fire this same event.
    await cog.on_voice_state_update(FakeMember(g), state(vc1, mute=False),
                                    state(vc1, mute=True))
    assert posted() == {}, "toggling your own mic is not movement"
    print("  a mute toggle produced nothing OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    music = FakeMember(g, uid=999)
    music.bot = True
    await cog.on_voice_state_update(music, state(None), state(vc1))
    assert posted() == {}, "a music bot rejoining every track would bury everybody"
    print("  bots are left out OK")

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN,
             log_events=all_events(voice_activity={"on": True, "channel": OTHER}))
    g = make_guild()
    await cog.on_voice_state_update(FakeMember(g), state(None), state(vc1))
    assert posted() == {OTHER: 1}, posted()
    print("  and it routes to its own channel like everything else OK")

    print("\n=== a purge is one entry, not a hundred ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    who = FakeUser(600, "chatty")
    await cog.on_bulk_message_delete(
        [FakeMessage(g, CHANNELS[ROOM], who) for _ in range(40)])
    assert posted() == {MAIN: 1}, posted()
    assert "40" in CHANNELS[MAIN].sent[0]["embed"].description
    print("  40 messages summarised into one embed OK")

    print("\n=== a channel it can't post in doesn't raise ===")
    reset_channels(main_ok=False)
    settings(logging_enabled=True, log_channel=MAIN, log_events=all_events())
    g = make_guild()
    await cog.on_member_join(FakeMember(g))       # must not propagate Forbidden
    assert posted() == {}
    print("  swallowed and reported to the console OK")

    print("\n=== a deleted log channel doesn't raise either ===")
    reset_channels()
    settings(logging_enabled=True, log_channel=99999, log_events=all_events())
    g = make_guild()
    await cog.on_member_join(FakeMember(g))
    assert posted() == {}
    print("  unknown channel id handled OK")

    print("\n=== /logging status covers all four logs ===")
    class Resp:
        def __init__(self): self.calls = []; self.deferred = False
        async def defer(self, **kw): self.deferred = True
        async def send_message(self, *a, **kw): self.calls.append((a, kw))

    class Follow:
        def __init__(self): self.calls = []
        async def send(self, *a, **kw): self.calls.append((a, kw))

    def status_interaction():
        i = types.SimpleNamespace(guild=make_guild(), user=FakeUser(), response=Resp())
        i.followup = Follow()
        return i

    def status_embed(i):
        return i.followup.calls[0][1]["embed"]

    reset_channels()
    settings(logging_enabled=True, log_channel=MAIN,
             log_events=all_events(member_join={"on": True, "channel": OTHER},
                                   member_ban={"on": False, "channel": None}),
             modlog_channel=OTHER, medialog_enabled=True, medialog_channel=ROOM)
    i = status_interaction()
    await cog.status.callback(cog, i)
    embed = status_embed(i)
    names = [f.name for f in embed.fields]
    body = "\n".join(f.value for f in embed.fields)
    recording = len(L.EVENT_KEYS) - 1          # every event except the ban one switched off
    assert f"{recording} of {len(L.EVENT_KEYS)} on" in names[0], names[0]
    assert "<#901>" in body, body
    assert any("Bans" in f.value for f in embed.fields), body
    # The three that used to be their own commands now report in the same place.
    for heading in ("Moderation log", "Deleted media", "Survey reminders"):
        assert any(heading in n for n in names), (heading, names)
    assert "Off" in body, "the reminders log is off here and should say so"
    print(f"  server log {recording}/{len(L.EVENT_KEYS)}, plus moderation, media and "
          f"reminders in one embed OK")

    settings()
    i = status_interaction()
    await cog.status.callback(cog, i)
    body = "\n".join(f.value for f in status_embed(i).fields)
    assert "/logging setup" in body, body
    assert body.count("Off") >= 4, "all four should report as off on a fresh server"
    print("  a fresh server sees four logs, all off, and is pointed at /logging setup OK")

    # ── /logging setup ───────────────────────────────────────────────
    print("\n=== the groupings cover every event, once ===")
    covered = [k for _, keys in L.GROUPED for k in keys]
    assert sorted(covered) == sorted(L.EVENT_KEYS), \
        set(L.EVENT_KEYS).symmetric_difference(covered)
    assert len(covered) == len(set(covered)), "an event in two channels would log twice"
    print(f"  {len(L.GROUPED)} channels covering all {len(covered)} events, no overlaps OK")

    class FakeCategory:
        def __init__(self, name): self.name = name; self.text_channels = []

    class Me:
        """A stand-in for guild.me that can be a dict key.

        types.SimpleNamespace defines __eq__ and so is unhashable, but the real guild.me is a
        Member and gets used as a key in the permission overwrites.
        """
        def __init__(self, **perms):
            self.guild_permissions = types.SimpleNamespace(**perms)

    class SetupGuild:
        def __init__(self, can_channels=True, can_roles=True, fail_after=None,
                     categories=None):
            self.id = GUILD; self.name = "Cool Server"
            self.categories = categories or []
            self.default_role = "@everyone"
            self.me = Me(manage_channels=can_channels, manage_roles=can_roles,
                         view_audit_log=True)
            self.created = []; self.fail_after = fail_after; self.overwrites = "unset"
        async def create_category(self, name, overwrites=None, reason=None):
            self.overwrites = overwrites
            cat = FakeCategory(name); self.categories.append(cat); return cat
        async def create_text_channel(self, name, category=None, reason=None):
            if self.fail_after is not None and len(self.created) >= self.fail_after:
                raise discord.HTTPException(
                    types.SimpleNamespace(status=429, reason="Too Many Requests"),
                    "rate limited")
            ch = FakeChannel(1000 + len(self.created), name)
            self.created.append(ch); category.text_channels.append(ch); return ch

    class Follow:
        def __init__(self): self.calls = []
        async def send(self, *a, **kw): self.calls.append((a, kw))

    class DeferResp:
        def __init__(self): self.calls = []; self.deferred = False
        async def defer(self, **kw): self.deferred = True
        async def send_message(self, *a, **kw): self.calls.append((a, kw))

    def setup_interaction(guild):
        i = types.SimpleNamespace(guild=guild, user=FakeUser(7, "tet"), response=DeferResp())
        i.followup = Follow()
        return i

    def said_setup(i):
        if i.followup.calls:
            args, kw = i.followup.calls[0]
        else:
            args, kw = i.response.calls[0]
        return kw.get("embed") or (args[0] if args else kw.get("content", ""))

    def choice(value):
        return discord.app_commands.Choice(name=value, value=value)

    print("\n=== setup, grouped ===")
    settings()
    g = SetupGuild()
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=None, category=None)
    assert [c.name for c in g.created] == [n for n, _ in L.GROUPED], [c.name for c in g.created]
    assert g.categories[0].name == "Server Logs"
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["logging_enabled"] is True
    assert cfg["log_channel"] is None, "each channel names its own events"
    assert all(e["on"] for e in cfg["log_events"].values())
    by_name = {c.name: c.id for c in g.created}
    assert cfg["log_events"]["message_delete"]["channel"] == by_name["message-log"]
    assert cfg["log_events"]["voice_activity"]["channel"] == by_name["voice-log"]
    assert cfg["modlog_channel"] == by_name["moderation-log"], cfg.get("modlog_channel")
    print(f"  built {len(g.created)} channels, routed all {len(L.EVENT_KEYS)} events, "
          f"mod cases to #moderation-log OK")

    print("\n=== the category is hidden from everybody else ===")
    assert g.overwrites is not None and g.default_role in g.overwrites
    assert g.overwrites[g.default_role].view_channel is False
    print("  @everyone denied View Channel OK")

    print("\n=== and it says so when it can't hide them ===")
    settings()
    g = SetupGuild(can_roles=False)
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=None, category=None)
    embed = said_setup(i)
    notes = "\n".join(f.value for f in embed.fields)
    assert "visible to everyone" in notes, notes
    assert g.overwrites is None, "can't set overwrites it doesn't have the permission for"
    print("  warned rather than quietly building a public deleted-messages log OK")

    print("\n=== setup, one channel ===")
    settings()
    g = SetupGuild()
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=choice("single"), category=None)
    assert len(g.created) == 1, [c.name for c in g.created]
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["log_channel"] == g.created[0].id
    assert all(e["on"] for e in cfg["log_events"].values())
    print("  one channel, everything pointed at it OK")

    print("\n=== setup, one per event ===")
    settings()
    g = SetupGuild()
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=choice("each"), category=None)
    assert len(g.created) == len(L.EVENT_KEYS), len(g.created)
    cfg = await GuildConfig.get(bot, GUILD)
    assert len({e["channel"] for e in cfg["log_events"].values()}) == len(L.EVENT_KEYS)
    print(f"  {len(g.created)} channels, one per event, all distinct OK")

    print("\n=== running it twice doesn't duplicate anything ===")
    settings()
    g = SetupGuild()
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=None, category=None)
    first = list(g.created)
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=None, category=None)
    assert g.created == first, "the second run should reuse, not rebuild"
    assert len(g.categories) == 1, g.categories
    notes = "\n".join(f.value for f in said_setup(i).fields)
    assert "Reused" in notes, notes
    print("  second run reused the category and all five channels OK")

    print("\n=== a custom category name ===")
    settings()
    g = SetupGuild()
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=None, category="  Staff Only  ")
    assert g.categories[0].name == "Staff Only", g.categories[0].name
    print("  trimmed and used OK")

    print("\n=== no Manage Channels ===")
    settings()
    g = SetupGuild(can_channels=False)
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=None, category=None)
    assert not i.response.deferred, "should refuse before doing anything"
    assert "Manage Channels" in said_setup(i)
    assert g.created == []
    cfg = await GuildConfig.get(bot, GUILD)
    assert not cfg.get("logging_enabled")
    print("  refused up front, nothing created or saved OK")

    print("\n=== giving up part way keeps what it made ===")
    settings()
    g = SetupGuild(fail_after=2)
    i = setup_interaction(g)
    await cog.setup_logs.callback(cog, i, style=None, category=None)
    assert len(g.created) == 2, len(g.created)
    cfg = await GuildConfig.get(bot, GUILD)
    on = [k for k, v in cfg["log_events"].items() if v["on"]]
    assert len(on) == 7, on          # message-log covers 3, member-log covers 4
    assert all(not v["on"] for k, v in cfg["log_events"].items() if k not in on)
    notes = "\n".join(f.value for f in said_setup(i).fields)
    assert "stop early" in notes, notes
    print(f"  2 channels built, {len(on)} events routed, the rest left off and reported OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
