"""Reminders: parsing how long, keeping the queue, and delivering once.

The parser is most of the risk. "2h" and "2 hours and a bit" both look like a person asking
for the same thing, and only one of them is a request that can be honoured. Hearing the first
half of the second one would set a reminder nobody asked for, at a time they never said.

The other half is the delivery loop, where the thing that matters is that a reminder goes out
exactly once. Claiming before sending loses one on a crash; claiming after would resend every
reminder in flight on every restart.
"""
import pathlib as _pathlib
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")

import asyncio, datetime, sys, types
sys.path.insert(0, SRC_DIR)


class _Cursor(list):
    def sort(self, key, direction=1):
        return _Cursor(sorted(self, key=lambda d: (d.get(key) is not None, d.get(key)),
                              reverse=direction < 0))
    def limit(self, n):
        return _Cursor(self[:n])


class FakeColl:
    def __init__(self, name):
        self.name = name
        self.docs = []
        self._ids = 0

    def create_index(self, *a, **k): pass

    def _match(self, doc, query):
        for key, value in query.items():
            if isinstance(value, dict) and "$lte" in value:
                if not (doc.get(key) is not None and doc[key] <= value["$lte"]):
                    return False
            elif doc.get(key) != value:
                return False
        return True

    def find(self, query=None, *a, **k):
        return _Cursor(d for d in self.docs if self._match(d, query or {}))

    def find_one(self, query, *a, **k):
        return next((d for d in self.docs if self._match(d, query)), None)

    def count_documents(self, query, *a, **k):
        return sum(1 for d in self.docs if self._match(d, query))

    def insert_one(self, doc):
        self._ids += 1
        stored = dict(doc, _id=self._ids)
        self.docs.append(stored)
        return types.SimpleNamespace(inserted_id=stored["_id"])

    def delete_one(self, query):
        for i, doc in enumerate(self.docs):
            if self._match(doc, query):
                del self.docs[i]
                return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)

    def find_one_and_delete(self, query):
        for i, doc in enumerate(self.docs):
            if self._match(doc, query):
                return self.docs.pop(i)
        return None


class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, FakeColl(n))


DB = FakeDB()
stub = types.ModuleType("Database"); stub.get_bot_database = lambda c: DB
sys.modules["Database"] = stub
for name in ("pymongo", "certifi", "dotenv"):
    mod = types.ModuleType(name)
    if name == "pymongo": mod.MongoClient = lambda *a, **k: object()
    if name == "certifi": mod.where = lambda: ""
    if name == "dotenv": mod.load_dotenv = lambda *a, **k: None
    sys.modules[name] = mod

import discord
from discord.ext import commands

GUILD, CHANNEL, ME = 900, 901, 7


class Reply:
    def __init__(self): self.sent = []
    async def send_message(self, content=None, *, embed=None, ephemeral=False, **k):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})
    send = send_message

    @property
    def text(self):
        out = []
        for item in self.sent:
            if item["content"]:
                out.append(str(item["content"]))
            if item["embed"] is not None:
                e = item["embed"]
                out.append(f"{e.title or ''} {e.description or ''} "
                           f"{e.footer.text if e.footer else ''}")
        return "\n".join(out)


class FakeInteraction:
    def __init__(self, user_id=ME):
        self.user = types.SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.guild = types.SimpleNamespace(id=GUILD, name="Test Server")
        self.channel_id = CHANNEL
        self.response = Reply()


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Reminders")
    cog = bot.get_cog("Reminders")
    R = sys.modules["Cogs.Reminders"]
    cog.deliver.cancel()          # driven by hand below rather than on a timer

    print("=== how long is that, then ===")
    for text, expected in (("10m", 600), ("2h", 7200), ("2h30m", 9000), ("3d", 259200),
                           ("1w", 604800), ("45s", 45), ("1d 12h", 129600),
                           ("1h 30m 15s", 5415), ("2 h", 7200), ("10M", 600)):
        got = R.parse_delay(text)
        assert got == expected, (text, got, expected)
    print("  every shape of duration, including spaces and capitals OK")

    print("\n=== and what isn't one ===")
    for bad in ("", None, "tomorrow", "soon", "2h and a bit", "next tuesday",
                "10", "m", "-5m", "0s",
                "10s",                     # under the floor
                "400d",                    # over the ceiling
                "2h drop table"):
        assert R.parse_delay(bad) is None, (bad, R.parse_delay(bad))
    print("  words, empty, out of range, and anything with leftovers refused OK")
    # The leftovers rule is the one worth stating: hearing "2h" out of "2h and a bit" would
    # set a reminder at a time the person never asked for.
    assert R.parse_delay("2h and a bit") is None

    print("\n=== setting one ===")
    i = FakeInteraction()
    await cog.remindme.callback(cog, i, "2h", "water the plants")
    assert len(DB["reminders"].docs) == 1, DB["reminders"].docs
    saved = DB["reminders"].docs[0]
    assert saved["text"] == "water the plants"
    assert saved["user_id"] == ME and saved["guild_id"] == GUILD
    assert saved["channel_id"] == CHANNEL, "so it has somewhere to fall back to"
    ahead = (saved["due"] - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    assert 7100 < ahead < 7250, ahead
    # Only the person who asked needs to see it.
    assert i.response.sent[0]["ephemeral"] is True
    print(f"  saved, due in {ahead / 3600:.1f}h, and told only them OK")

    print("\n=== a duration it can't read saves nothing ===")
    i = FakeInteraction()
    await cog.remindme.callback(cog, i, "whenever", "something")
    assert len(DB["reminders"].docs) == 1, "nothing new"
    assert "couldn't read" in i.response.text, i.response.text
    assert i.response.sent[0]["ephemeral"] is True
    print("  says so, quietly, rather than guessing OK")

    i = FakeInteraction()
    await cog.remindme.callback(cog, i, "1h", "   ")
    assert len(DB["reminders"].docs) == 1, "an empty reminder is not a reminder"
    print("  and neither does an empty one OK")

    print("\n=== the queue has a ceiling ===")
    for n in range(R.MAX_PENDING - 1):
        await cog.remindme.callback(cog, FakeInteraction(), "1h", f"thing {n}")
    assert len(DB["reminders"].docs) == R.MAX_PENDING
    i = FakeInteraction()
    await cog.remindme.callback(cog, i, "1h", "one too many")
    assert len(DB["reminders"].docs) == R.MAX_PENDING, "the limit has to actually hold"
    assert "limit" in i.response.text
    # Somebody else is unaffected: the cap is per person, not per server.
    other = FakeInteraction(user_id=ME + 1)
    await cog.remindme.callback(cog, other, "1h", "mine")
    assert len(DB["reminders"].docs) == R.MAX_PENDING + 1
    print(f"  {R.MAX_PENDING} each, and one person filling up doesn't block anyone else OK")

    print("\n=== seeing and cancelling your own ===")
    i = FakeInteraction()
    await cog.reminders.callback(cog, i, None)
    listing = i.response.text
    assert "water the plants" in listing and "**1.**" in listing
    assert i.response.sent[0]["ephemeral"] is True
    print("  numbered, and only they see the list OK")

    # Soonest first, so number 1 is whichever is due next, not whichever was set first.
    mine = sorted((d for d in DB["reminders"].docs if d["user_id"] == ME),
                  key=lambda d: d["due"])
    first, last = mine[0], mine[-1]
    assert last["text"] == "water the plants", "the 2h one is furthest away, so it sorts last"

    before = len(DB["reminders"].docs)
    i = FakeInteraction()
    await cog.reminders.callback(cog, i, 1)
    assert len(DB["reminders"].docs) == before - 1
    assert first["_id"] not in [d["_id"] for d in DB["reminders"].docs], \
        "number 1 is the one due soonest, and that is what has to go"
    assert any(d["_id"] == last["_id"] for d in DB["reminders"].docs), \
        "and nothing else moved"
    print("  cancelling by its number removes the one due soonest OK")

    print("\n=== and you can't cancel somebody else's ===")
    # Numbers are positions in your own list, so there is no number that reaches anybody
    # else's. Asking for one out of range says so rather than reaching past the end.
    stranger = FakeInteraction(user_id=ME + 99)
    before = len(DB["reminders"].docs)
    await cog.reminders.callback(cog, stranger, 1)
    assert len(DB["reminders"].docs) == before, "somebody with none must delete none"
    assert "no reminder" in stranger.response.text
    print("  a number nobody has cancels nothing OK")

    print("\n=== delivery, once and only once ===")
    DB["reminders"].docs.clear()
    sent = []

    class FakeUser:
        def __init__(self, uid): self.id, self.mention = uid, f"<@{uid}>"
        async def send(self, **kwargs): sent.append(("dm", kwargs))

    bot.get_user = lambda uid: FakeUser(uid)
    bot.get_guild = lambda gid: types.SimpleNamespace(name="Test Server")
    bot.get_channel = lambda cid: None

    now = datetime.datetime.now(datetime.timezone.utc)
    DB["reminders"].docs.extend([
        {"_id": 1, "user_id": ME, "guild_id": GUILD, "channel_id": CHANNEL,
         "text": "due now", "due": now - datetime.timedelta(seconds=5), "set_at": now},
        {"_id": 2, "user_id": ME, "guild_id": GUILD, "channel_id": CHANNEL,
         "text": "not yet", "due": now + datetime.timedelta(hours=1), "set_at": now},
    ])
    await cog.deliver()
    assert len(sent) == 1, sent
    assert "due now" in sent[0][1]["embed"].description
    assert [d["text"] for d in DB["reminders"].docs] == ["not yet"], DB["reminders"].docs
    print("  the due one went by DM, the future one stayed put OK")

    # Running again must not send it a second time, which is the whole reason it is claimed
    # out of the collection before it is sent rather than after.
    await cog.deliver()
    assert len(sent) == 1, "a delivered reminder must not come round again"
    print("  and a second pass sends nothing OK")

    print("\n=== closed DMs fall back to the channel ===")
    posted = []

    class ClosedUser(FakeUser):
        async def send(self, **kwargs):
            raise discord.HTTPException(
                types.SimpleNamespace(status=403, reason=""), "cannot send")

    class FakeChannel:
        async def send(self, **kwargs): posted.append(kwargs)

    bot.get_user = lambda uid: ClosedUser(uid)
    bot.get_channel = lambda cid: FakeChannel()
    DB["reminders"].docs.append(
        {"_id": 3, "user_id": ME, "guild_id": GUILD, "channel_id": CHANNEL,
         "text": "shout it then", "due": now - datetime.timedelta(seconds=1), "set_at": now})
    await cog.deliver()
    assert len(posted) == 1, posted
    assert posted[0]["content"] == f"<@{ME}>", "it has to ping them, or they'll never see it"
    assert "shout it then" in posted[0]["embed"].description
    print("  posted in the channel with a mention OK")

    print("\n=== and a channel that's gone too loses nothing else ===")
    bot.get_channel = lambda cid: None
    DB["reminders"].docs.append(
        {"_id": 4, "user_id": ME, "guild_id": GUILD, "channel_id": CHANNEL,
         "text": "nowhere to go", "due": now - datetime.timedelta(seconds=1), "set_at": now})
    await cog.deliver()          # must not raise
    assert not any(d["_id"] == 4 for d in DB["reminders"].docs), \
        "it is still claimed rather than retried forever"
    assert [d["text"] for d in DB["reminders"].docs] == ["not yet"], \
        "and the one that isn't due yet is untouched"
    print("  dropped quietly instead of jamming the queue OK")

    print("\nALL CHECKS PASSED")


asyncio.run(main())
