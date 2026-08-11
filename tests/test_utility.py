"""/say, and the two things about it that are easy to get wrong.

The first is what it is allowed to ping. /say needs Manage Messages, a permission plenty of
moderators hold and which does not include Mention Everyone. The bot does have Mention
Everyone, so a /say that passes text straight through lets a moderator ping the whole server
through the bot without holding the permission themselves. The allowed mentions are mirrored off
the person running the command for that reason, and these checks are what stop that regressing.

The second is the reply option. A message id has to travel as a string: Discord sends integer
command options as JSON numbers, and a snowflake is bigger than 2^53, so an integer option
arrives having lost its last few digits and points at nothing.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")

import asyncio, sys, types
sys.path.insert(0, SRC_DIR)

st = types.ModuleType("Database"); st.get_bot_database = lambda c: None
sys.modules["Database"] = st
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = lambda *a, **k: object()
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import inspect

import discord
from discord.ext import commands

CHAN, OTHER = 222, 333
MSG = 1534014704060596456          # a real-shaped snowflake, past 2^53

# What discord.py's own send() will accept. Anything else is a TypeError at runtime.
SEND_PARAMS = set(inspect.signature(discord.abc.Messageable.send).parameters)


class FakeChannel:
    def __init__(self, cid=CHAN, holds=(MSG,), readable=True):
        self.id = cid
        self.holds = set(holds)
        self.readable = readable
        self.sent = []

    async def fetch_message(self, mid):
        if not self.readable:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason=""), "no history")
        if mid not in self.holds:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason=""), "gone")
        return types.SimpleNamespace(
            id=mid, author=types.SimpleNamespace(display_name="someone"),
            # Real Messages carry this, and the tolerance for a since-deleted target lives on
            # the reference rather than on send().
            to_reference=lambda **kw: {"message_id": mid, **kw})

    async def send(self, content=None, **kw):
        # Checked against the real signature rather than a list written here, so this keeps
        # working across discord.py upgrades. A fake that took **kw and asked no questions is
        # how `fail_if_not_exists=False` reached production as a TypeError: every test passed
        # and the first real reply crashed.
        unknown = sorted(set(kw) - SEND_PARAMS)
        assert not unknown, f"Messageable.send() takes no {unknown}"
        self.sent.append({"content": content, **kw})
        return types.SimpleNamespace(id=1)


class Resp:
    def __init__(self): self.sent = []
    async def send_message(self, content=None, **kw):
        self.sent.append({"content": content, **kw})


def interaction(channel, mention_everyone=False):
    return types.SimpleNamespace(
        channel=channel,
        response=Resp(),
        guild=types.SimpleNamespace(id=17),
        user=types.SimpleNamespace(
            id=99, guild_permissions=types.SimpleNamespace(
                mention_everyone=mention_everyone)))


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=42, name="newt", avatar=None)
    await bot.load_extension("Cogs.utility")
    cog = bot.get_cog("Utility")
    U = sys.modules["Cogs.utility"]

    print("=== a message reference is read from a link or a bare id ===")
    for raw, want in (
        (str(MSG), (None, MSG)),
        (f"  {MSG}  ", (None, MSG)),
        (f"https://discord.com/channels/17/{CHAN}/{MSG}", (CHAN, MSG)),
        (f"https://discord.com/channels/17/{CHAN}/{MSG}/", (CHAN, MSG)),
        (f"https://canary.discord.com/channels/17/{CHAN}/{MSG}", (CHAN, MSG)),
        (f"https://ptb.discordapp.com/channels/17/{CHAN}/{MSG}", (CHAN, MSG)),
        (f"discord.com/channels/17/{CHAN}/{MSG}", (CHAN, MSG)),
        # A direct message link carries @me where the guild id goes.
        (f"https://discord.com/channels/@me/{CHAN}/{MSG}", (CHAN, MSG)),
        # Not a reference at all.
        ("", (None, None)),
        ("hello", (None, None)),
        ("12345", (None, None)),                       # too short to be a snowflake
        ("1" * 25, (None, None)),                      # too long
        ("https://example.com/channels/1/2/3", (None, None)),
    ):
        got = U.parse_message_ref(raw)
        assert got == want, (raw, got, want)
    print(f"  {MSG} survives as an int, links and junk both handled OK")

    print("\n=== the id keeps every digit ===")
    # The whole reason this option is a string. int(float(...)) is what an integer option does
    # to a snowflake on the way through JSON.
    assert U.parse_message_ref(str(MSG))[1] == MSG
    assert int(float(MSG)) != MSG, "if this ever passes, the precision worry is gone"
    print(f"  {MSG} not {int(float(MSG))} OK")

    print("\n=== replying attaches the message it answers ===")
    chan = FakeChannel()
    i = interaction(chan)
    await cog.say.callback(cog, i, "over here", reply=str(MSG))
    assert len(chan.sent) == 1, chan.sent
    assert chan.sent[0]["content"] == "over here"
    ref = chan.sent[0]["reference"]
    assert ref["message_id"] == MSG, ref
    # A reply to a message deleted between the lookup and the send must still go out, and that
    # tolerance belongs to the reference. Passing it to send() is a TypeError that only fires
    # when somebody actually replies, which is exactly how it reached production.
    assert ref["fail_if_not_exists"] is False, ref
    assert "fail_if_not_exists" not in chan.sent[0], "send() has no such argument"
    assert "reply" in i.response.sent[0]["content"].lower()
    assert i.response.sent[0]["ephemeral"] is True
    print(f"  {i.response.sent[0]['content']} OK")

    print("\n=== and without the option it is an ordinary message ===")
    chan = FakeChannel()
    i = interaction(chan)
    await cog.say.callback(cog, i, "just saying")
    assert chan.sent[0]["reference"] is None
    assert "reply" not in i.response.sent[0]["content"].lower()
    print("  no reference, and the confirmation says so OK")

    print("\n=== a message that isn't there is explained, not sent anyway ===")
    chan = FakeChannel(holds=())
    i = interaction(chan)
    await cog.say.callback(cog, i, "hi", reply=str(MSG))
    assert not chan.sent, "nothing should be posted"
    assert "no message with that id" in i.response.sent[0]["content"].lower()
    print("  deleted or wrong id refused OK")

    print("\n=== a link to another channel is refused before Discord refuses it ===")
    chan = FakeChannel()
    i = interaction(chan)
    await cog.say.callback(cog, i, "hi",
                           reply=f"https://discord.com/channels/17/{OTHER}/{MSG}")
    assert not chan.sent
    body = i.response.sent[0]["content"]
    assert f"<#{OTHER}>" in body and "same channel" in body
    print(f"  {body} OK")

    print("\n=== unreadable history says which permission is missing ===")
    chan = FakeChannel(readable=False)
    i = interaction(chan)
    await cog.say.callback(cog, i, "hi", reply=str(MSG))
    assert not chan.sent
    assert "read message history" in i.response.sent[0]["content"].lower()
    print("  Read Message History named OK")

    print("\n=== nonsense in the reply option is refused ===")
    chan = FakeChannel()
    i = interaction(chan)
    await cog.say.callback(cog, i, "hi", reply="the second one")
    assert not chan.sent
    assert "copy message link" in i.response.sent[0]["content"].lower()
    print("  told what to paste instead OK")

    print("\n=== /say cannot lend the bot's Mention Everyone to somebody ===")
    chan = FakeChannel()
    i = interaction(chan, mention_everyone=False)
    await cog.say.callback(cog, i, "@everyone get in here")
    allowed = chan.sent[0]["allowed_mentions"]
    assert allowed.everyone is False, "a moderator without the permission must not ping everyone"
    assert allowed.roles is False, "nor a role"
    assert allowed.users is True, "pinging one person by name is fine"
    assert allowed.replied_user is False, "a reply doesn't ping unless it is asked to"
    print("  everyone and roles blocked, users allowed, reply ping off OK")

    print("\n=== but somebody who holds it keeps it ===")
    chan = FakeChannel()
    i = interaction(chan, mention_everyone=True)
    await cog.say.callback(cog, i, "@everyone get in here")
    allowed = chan.sent[0]["allowed_mentions"]
    assert allowed.everyone is True and allowed.roles is True
    assert allowed.replied_user is False, "still not the replied-to user by default"
    print("  an admin with Mention Everyone can still use it OK")

    print("\n=== the option has to be a string on the command itself ===")
    params = {p.name: p for p in cog.say.parameters}
    assert set(params) == {"message", "reply", "ping"}, list(params)
    assert params["reply"].required is False, "replying is optional"
    assert params["reply"].type is discord.AppCommandOptionType.string, params["reply"].type
    assert params["ping"].required is False, "pinging is optional and off by default"
    assert params["ping"].type is discord.AppCommandOptionType.boolean, params["ping"].type
    print(f"  reply is an optional {params['reply'].type.name}, "
          f"ping an optional {params['ping'].type.name} OK")

    print("\n=== ping decides whether the reply notifies anybody ===")
    # The whole point of the option: same command, same reply, one of them lands on somebody's
    # phone and the other does not.
    for wanted in (False, True):
        chan = FakeChannel()
        i = interaction(chan)
        await cog.say.callback(cog, i, "answered", reply=str(MSG), ping=wanted)
        allowed = chan.sent[0]["allowed_mentions"]
        assert allowed.replied_user is wanted, (wanted, allowed.replied_user)
        ref = chan.sent[0]["reference"]
        assert ref is not None, "and it is still a real reply"
        # The reply must survive its target being deleted between lookup and send.
        assert ref["fail_if_not_exists"] is False, ref
        said = i.response.sent[0]["content"].lower()
        # Both confirmations mention pinging, because the useful thing to say is which of the
        # two happened. So check for the one that did.
        assert ("they were pinged" in said) is wanted, said
        assert ("nobody was pinged" in said) is not wanted, said
        print(f"  ping={wanted} -> replied_user={allowed.replied_user}, said {said!r}")

    print("\n=== ping on its own is refused rather than ignored ===")
    # Silently dropping it would leave somebody believing they had sent a notification.
    chan = FakeChannel()
    i = interaction(chan)
    await cog.say.callback(cog, i, "hello", ping=True)
    assert not chan.sent, "nothing should be sent"
    assert "only does anything alongside" in i.response.sent[0]["content"]
    print("  told that ping needs reply, and nothing sent OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
