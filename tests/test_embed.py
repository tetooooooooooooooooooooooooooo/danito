"""The message builder: turning a long form into something Discord will accept.

Almost all the risk is in the validation. Discord answers a bad message by refusing the whole
thing and naming the field in a nested error tree, so anything caught here is a mistake fixed
in the form rather than a 400 to decipher. The rest is the usual: the channel has to belong to
this server, and nothing should ping unless somebody asked for it.
"""
import pathlib as _pathlib
ROOT = _pathlib.Path(__file__).resolve().parents[1]
WEB_DIR = str(ROOT / "web")

import html, json, os, sys, types
sys.path.insert(0, WEB_DIR)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",
})


class _Cursor(list):
    """list.sort exists and takes no positional arguments, so a bare list standing in for a
    cursor fails in a way that looks nothing like the real problem."""
    def sort(self, key, direction=1): return self
    def limit(self, n): return _Cursor(self[:n])


class FakeColl:
    def __init__(self, name): self.name = name
    def find(self, *a, **k): return _Cursor()
    def find_one(self, q=None, *a, **k):
        return {"_id": "bot", "guild_ids": [111]} if self.name == "runtime" else None
    def update_one(self, *a, **k): return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

import discord_api as api
CHANNELS = [{"id": "900", "name": "general", "type": 0, "position": 0},
            {"id": "901", "name": "announcements", "type": 5, "position": 1}]
api.manageable_guilds = lambda t, u, force=False: [{"id": "111", "name": "Test", "icon": None}]
api.guild_channels = lambda gid: list(CHANNELS)
api.guild_roles = lambda gid: []

SENT = []
api.post_message = lambda channel_id, payload: (
    SENT.append((channel_id, payload)) or {"id": "1"})

import app as dashboard
dashboard.app.config["TESTING"] = True


class Form(dict):
    """Flask's form object, as much of it as the cleaner touches."""
    def get(self, key, default=None):
        return dict.get(self, key, default)


def clean(**fields):
    return store.clean_embed(Form(fields))


def login(client):
    with client.session_transaction() as s:
        s["user"] = {"id": "7", "username": "Admin", "avatar": ""}
        s["token"] = "user-token"
        s["csrf"] = "test-csrf"


def send(client, **fields):
    fields.setdefault("csrf", "test-csrf")
    fields.setdefault("channel_id", "900")
    return client.post("/servers/111/embed", data=fields, follow_redirects=True)


def main():
    c = dashboard.app.test_client()
    login(c)

    print("=== a plain message ===")
    payload, problems = clean(content="hello everyone")
    assert not problems, problems
    assert payload["content"] == "hello everyone"
    assert "embeds" not in payload, "no embed means no embed, not an empty one"
    print("  text only, and no empty embed tacked on OK")

    print("\n=== a full embed ===")
    payload, problems = clean(
        title="A title", url="https://example.test", description="Some **words**",
        colour="#3ddc97", author_name="Someone", author_url="https://example.test/a",
        author_icon="https://example.test/i.png", footer_text="At the bottom",
        footer_icon="https://example.test/f.png", thumbnail="https://example.test/t.png",
        image="https://example.test/big.png", timestamp="on",
        field_name_0="First", field_value_0="one", field_inline_0="on",
        field_name_1="Second", field_value_1="two")
    assert not problems, problems
    embed = payload["embeds"][0]
    assert embed["title"] == "A title" and embed["url"] == "https://example.test"
    assert embed["color"] == 0x3DDC97, hex(embed["color"])
    assert embed["author"] == {"name": "Someone", "url": "https://example.test/a",
                               "icon_url": "https://example.test/i.png"}
    assert embed["footer"] == {"text": "At the bottom",
                               "icon_url": "https://example.test/f.png"}
    assert embed["thumbnail"]["url"].endswith("t.png")
    assert embed["image"]["url"].endswith("big.png")
    assert embed["timestamp"], "the timestamp is set at send time, not typed"
    assert embed["fields"] == [{"name": "First", "value": "one", "inline": True},
                               {"name": "Second", "value": "two", "inline": False}]
    print("  every part carried through, and inline only where it was ticked OK")

    print("\n=== the colour, three ways ===")
    assert clean(title="x", colour="3ddc97")[0]["embeds"][0]["color"] == 0x3DDC97
    assert "color" not in clean(title="x", colour="")[0]["embeds"][0], "blank means no colour"
    _, problems = clean(title="x", colour="mint")
    assert problems and "hex" in problems[0], problems
    print("  with or without the hash, blank, and a word refused OK")

    print("\n=== links have to be links ===")
    for field, label in (("url", "title link"), ("image", "image"),
                         ("thumbnail", "thumbnail"), ("author_icon", "author icon"),
                         ("footer_icon", "footer icon")):
        _, problems = clean(title="x", author_name="a", footer_text="f",
                            **{field: "example.test/thing.png"})
        assert problems, field
        assert any("http://" in p for p in problems), (field, problems)
    print("  a bare domain is caught here rather than by Discord refusing the message OK")

    print("\n=== and the pieces that need a partner ===")
    _, problems = clean(title="", url="https://example.test", description="x")
    assert any("needs a title" in p for p in problems), problems
    _, problems = clean(description="x", author_icon="https://example.test/i.png")
    assert any("author name" in p for p in problems), problems
    _, problems = clean(description="x", footer_icon="https://example.test/f.png")
    assert any("footer text" in p for p in problems), problems
    _, problems = clean(description="x", field_name_0="Only a name")
    assert any("both a name and some text" in p for p in problems), problems
    print("  a link with no title, an icon with no name, half a field, all named OK")

    print("\n=== every length limit, with the number in the message ===")
    for field, limit in (("content", store.EMBED_MAX["content"]),
                         ("title", store.EMBED_MAX["title"]),
                         ("description", store.EMBED_MAX["description"]),
                         ("author_name", store.EMBED_MAX["author"]),
                         ("footer_text", store.EMBED_MAX["footer"])):
        _, problems = clean(**{field: "x" * (limit + 1)})
        assert problems, field
        assert str(limit) in problems[0], (field, problems)
    _, problems = clean(field_name_0="x" * 257, field_value_0="y")
    assert problems and "256" in problems[0], problems
    _, problems = clean(field_name_0="n", field_value_0="y" * 1025)
    assert problems and "1024" in problems[0], problems
    print("  each one says what it is and what the limit was OK")

    print("\n=== and the shared ceiling nothing warns you about ===")
    # Each piece is legal on its own. Together they are over Discord's 6000 across the lot.
    payload, problems = clean(title="t" * 250, description="d" * 4000,
                              footer_text="f" * 2000)
    assert any("6000" in p for p in problems), problems
    print(f"  {problems[-1][:70]}… OK")

    print("\n=== 25 fields is the ceiling, and the 26th is ignored ===")
    many = {}
    for n in range(30):
        many[f"field_name_{n}"] = f"n{n}"
        many[f"field_value_{n}"] = f"v{n}"
    payload, problems = clean(**many)
    assert not problems, problems
    assert len(payload["embeds"][0]["fields"]) == store.MAX_EMBED_FIELDS
    assert payload["embeds"][0]["fields"][-1]["name"] == "n24"
    print(f"  {store.MAX_EMBED_FIELDS} kept, the rest never reach Discord OK")

    print("\n=== an empty form sends nothing ===")
    _, problems = clean()
    assert any("nothing to send" in p.lower() for p in problems), problems
    # A colour on its own is an invisible message, which Discord refuses with a 400.
    _, problems = clean(colour="#3ddc97")
    assert any("nothing to send" in p.lower() for p in problems), problems
    print("  including a form with only a colour in it OK")

    print("\n=== nothing pings unless it was asked to ===")
    payload, _ = clean(content="@everyone come here")
    assert payload["allowed_mentions"] == {"parse": []}, payload["allowed_mentions"]
    payload, _ = clean(content="@everyone come here", allow_pings="on")
    assert "everyone" in payload["allowed_mentions"]["parse"]
    print("  off by default, so a pasted draft can't wake a server up OK")

    print("\n=== the page renders, and only for somebody who runs the server ===")
    r = c.get("/servers/111/embed")
    assert r.status_code == 200, r.status_code
    body = html.unescape(r.data.decode())
    assert "Message builder" in body
    assert "#general" in body and "#announcements" in body, "it needs a channel picker"
    assert 'data-embed-form' in body and 'data-preview-embed' in body
    assert "test-csrf" in body

    anon = dashboard.app.test_client()
    assert anon.get("/servers/111/embed").status_code == 302
    assert c.get("/servers/999/embed").status_code == 404
    print("  builder and preview present, logged out redirects, other servers 404 OK")

    print("\n=== sending it ===")
    SENT.clear()
    r = send(c, content="hello", title="Hi", description="there")
    assert r.status_code == 200, r.status_code
    assert len(SENT) == 1, SENT
    channel_id, payload = SENT[0]
    assert channel_id == 900 and payload["content"] == "hello"
    assert payload["embeds"][0]["title"] == "Hi"
    assert "Sent to #general" in html.unescape(r.data.decode())
    print("  posted to the right channel, and said so OK")

    print("\n=== and only a successful send says to bin the draft ===")
    # The browser keeps what you typed in local storage, and only throws it away when this
    # marker comes back. Anything else and a refused message would take your work with it.
    SENT.clear()
    r = c.post("/servers/111/embed", data={"csrf": "test-csrf", "channel_id": "900",
                                           "content": "hello"})
    assert r.status_code == 302 and "sent=1" in r.headers["Location"], r.headers["Location"]
    r = c.post("/servers/111/embed", data={"csrf": "test-csrf", "channel_id": "900",
                                           "title": "x" * 300})
    assert r.status_code == 302 and "sent=1" not in r.headers["Location"], \
        "a refused message must not look like a sent one"
    assert len(SENT) == 1, "and it must not have gone out"
    print("  sent=1 on success, absent on refusal OK")

    print("\n=== a channel in somebody else's server is refused ===")
    SENT.clear()
    r = send(c, channel_id="123456", content="hello")
    assert not SENT, "an id that isn't in this guild must never be posted to"
    assert "Pick a channel in this server" in html.unescape(r.data.decode())
    r = send(c, channel_id="not a number", content="hello")
    assert not SENT
    print("  a foreign id and a junk id both stopped before the send OK")

    print("\n=== a form with problems sends nothing and lists them all ===")
    SENT.clear()
    r = send(c, title="x" * 300, image="nonsense", colour="green")
    assert not SENT, "nothing goes out while anything is wrong"
    body = html.unescape(r.data.decode())
    assert body.count("flash") >= 3, "all of them at once, not one at a time"
    assert "256" in body and "http://" in body and "hex" in body
    print("  three problems, three messages, nothing sent OK")

    print("\n=== and a forged form post is refused ===")
    SENT.clear()
    r = c.post("/servers/111/embed", data={"channel_id": "900", "content": "hi"})
    assert r.status_code == 400, r.status_code
    assert not SENT
    print("  no csrf token, no message OK")

    print("\n=== Discord's own refusal is passed on in words ===")
    def refuse(channel_id, payload):
        raise api.DiscordError("embeds.0.image.url: Not a well formed URL")
    working, api.post_message = api.post_message, refuse
    try:
        r = send(c, content="hello")
        assert "Not a well formed URL" in html.unescape(r.data.decode())
    finally:
        api.post_message = working
    print("  the field Discord named is shown, not just a status code OK")

    print("\n=== which means unpicking Discord's error tree ===")
    explained = api._explain({"code": 50035, "message": "Invalid Form Body", "errors": {
        "embeds": {"0": {"image": {"url": {"_errors": [
            {"code": "URL_TYPE_INVALID_URL", "message": "Not a well formed URL"}]}}}}}})
    assert explained == "embeds.0.image.url: Not a well formed URL", explained
    # No errors tree at all, so the top level message is the best there is.
    assert api._explain({"message": "Missing Permissions"}) == "Missing Permissions"
    assert api._explain({}) == ""
    print(f"  {explained} OK")

    print("\n=== it's reachable from the settings sidebar ===")
    body = c.get("/servers/111").data.decode()
    assert "/servers/111/embed" in body
    assert 'data-tab="embed"' not in body, "it's a page, not a pane"
    print("  linked, and not registered as a tab OK")

    print("\nALL CHECKS PASSED")


main()
