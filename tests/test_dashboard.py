"""Dashboard security and behaviour.

The things that actually matter here are the refusals: an anonymous visitor, a logged in user
poking at a server they don't administer, a forged form post, and a channel id belonging to
somewhere else.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import os, sys, types

WEB = WEB_DIR
sys.path.insert(0, WEB)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",          # the test client doesn't speak TLS
})

# --- fake mongo -------------------------------------------------------------
SAVED = {}
DIRTY = set()
BOT_GUILDS = {111}


class Cursor(list):
    def limit(self, n): return Cursor(self[:n])
    def sort(self, *a, **k): return self


class FakeColl:
    def __init__(self, name): self.name = name
    def find(self, q=None, *a, **k):
        return Cursor()          # this suite has no role panels; test_dashboard_roles covers them
    def find_one(self, q, *a, **k):
        if self.name == "runtime":
            return {"_id": "bot", "guild_ids": sorted(BOT_GUILDS)}
        return SAVED.get(q.get("guild_id"))
    def update_one(self, q, ops, upsert=False):
        if self.name == "config_dirty":
            DIRTY.add(q["_id"]); return
        gid = q["guild_id"]
        SAVED.setdefault(gid, {"guild_id": gid}).update(ops.get("$set", {}))


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

# --- fake discord -----------------------------------------------------------
import discord_api as api

USER = {"id": "7", "username": "admin", "global_name": "Admin", "avatar": None}
# 111 they manage and the bot is in; 222 they manage but the bot isn't; 333 they do not manage.
USER_GUILDS = [
    {"id": "111", "name": "Managed", "icon": None, "owner": False, "permissions": str(0x20)},
    {"id": "222", "name": "No Bot", "icon": None, "owner": True, "permissions": "0"},
]
CHANNELS = [{"id": "900", "name": "general", "type": 0, "position": 0},
            {"id": "901", "name": "logs", "type": 0, "position": 1}]

api.get_user = lambda token: USER
api.manageable_guilds = lambda token, uid, force=False: list(USER_GUILDS)
api.guild_channels = lambda gid: list(CHANNELS) if gid == 111 else []
api.guild_roles = lambda gid: []
api.exchange_code = lambda code: {"access_token": "user-token"}

import app as dashboard
dashboard.app.config["TESTING"] = True


def login(client):
    with client.session_transaction() as s:
        s["user"] = {"id": "7", "username": "Admin", "avatar": ""}
        s["token"] = "user-token"


def csrf_of(client):
    with client.session_transaction() as s:
        s["csrf"] = "test-csrf"
    return "test-csrf"


def main():
    c = dashboard.app.test_client()

    print("=== anonymous visitors ===")
    r = c.get("/")
    assert r.status_code == 200 and b"Log in with Discord" in r.data
    for path in ("/servers", "/servers/111"):
        r = c.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], (path, r.status_code)
        print(f"  GET {path} -> redirected to login")
    r = c.post("/servers/111", data={"section": "medialog"})
    assert r.status_code == 302, r.status_code
    assert not SAVED, "an anonymous post must not write anything"
    print("  POST while logged out saved nothing OK")

    print("\n=== the OAuth callback needs its own state ===")
    r = c.get("/callback?code=abc&state=forged")
    assert r.status_code == 400, r.status_code
    print("  a state we never issued is refused OK")

    with c.session_transaction() as s:
        s["state"] = "real-state"
    r = c.get("/callback?code=abc&state=real-state")
    assert r.status_code == 302, r.status_code
    with c.session_transaction() as s:
        assert s.get("token") == "user-token"
        assert "state" not in s, "the state should be single use"
    print("  a matching state logs in and is then consumed OK")

    print("\n=== the header, once you're signed in ===")
    login(c)
    # The front page used to bounce anybody logged in to their server list, which made the
    # brand in the header a link back to the page you were already on and left no way to
    # reach the pitch, the prices or the feature list without logging out.
    r = c.get("/")
    assert r.status_code == 200, r.status_code
    body = r.data.decode()
    assert "Get found by people" in body, "the landing page itself, not a redirect"
    # It has to stop offering a login to somebody who is already logged in.
    assert "Log in with Discord" not in body
    assert "Open the dashboard" in body
    print("  / renders the landing page rather than redirecting OK")

    # And because it no longer redirects, the way back to the dashboard has to be in the
    # header on every page rather than implied by clicking the brand.
    for path in ("/", "/docs", "/premium", "/support", "/servers"):
        body = c.get(path).data.decode()
        assert ">Dashboard</a>" in body, path
        assert "Log out" in body, path
        # The button replaced the "Servers" link that used to sit in the left nav, rather
        # than joining it. Two links to the same page in one bar is just noise.
        assert 'nav-link" href="/servers"' not in body, path
    print("  a Dashboard button beside Log out, on every page OK")

    print("\n=== the picker only offers what it should ===")
    r = c.get("/servers")
    body = r.data.decode()
    assert "Managed" in body and "No Bot" in body
    assert body.index("Managed") < body.index("Not added yet") < body.index("No Bot")
    print("  servers with the bot listed first, the rest under 'Not added yet' OK")

    print("\n=== a guild the user does not manage ===")
    r = c.get("/servers/333")
    assert r.status_code == 404, r.status_code
    r = c.post("/servers/333", data={"section": "medialog", "csrf": csrf_of(c)})
    assert r.status_code == 404, r.status_code
    assert 333 not in SAVED, "must not write to a guild they don't administer"
    print("  GET and POST both 404, nothing written OK")

    print("\n=== a guild they manage but the bot isn't in ===")
    r = c.get("/servers/222")
    assert r.status_code == 404, r.status_code
    print("  404, since there is nothing to configure OK")

    print("\n=== forged form posts ===")
    login(c)
    csrf_of(c)
    r = c.post("/servers/111", data={"section": "medialog", "csrf": "wrong"})
    assert r.status_code == 400, r.status_code
    r = c.post("/servers/111", data={"section": "medialog"})
    assert r.status_code == 400, r.status_code
    assert 111 not in SAVED, "a bad token must not write"
    print("  wrong and missing tokens both refused, nothing written OK")

    print("\n=== a genuine save ===")
    token = csrf_of(c)
    r = c.post("/servers/111", data={
        "section": "medialog", "csrf": token,
        "medialog_enabled": "on", "medialog_channel": "900"})
    assert r.status_code == 302, r.status_code
    assert SAVED[111]["medialog_enabled"] is True
    assert SAVED[111]["medialog_channel"] == 900
    assert 111 in DIRTY, "the bot must be told its cached copy is stale"
    print(f"  saved {SAVED[111]} and flagged the guild dirty OK")

    print("\n=== a channel from somewhere else is rejected ===")
    r = c.post("/servers/111", data={
        "section": "medialog", "csrf": token,
        "medialog_enabled": "on", "medialog_channel": "999999"})
    assert r.status_code == 302
    assert SAVED[111]["medialog_channel"] is None, SAVED[111]
    assert SAVED[111]["medialog_enabled"] is False, "can't be on with nowhere to post"
    print("  unknown channel discarded and the feature switched off OK")

    print("\n=== unknown fields can't be smuggled in ===")
    r = c.post("/servers/111", data={
        "section": "medialog", "csrf": token, "medialog_channel": "900",
        "medialog_enabled": "on",
        "modlog_channel": "901", "welcome_message": "sneaky", "guild_id": "999"})
    assert r.status_code == 302
    assert SAVED[111].get("welcome_message") is None, SAVED[111]
    assert SAVED[111].get("modlog_channel") is None, "another section must be untouched"
    print("  only the submitted section's fields were written OK")

    print("\n=== an unknown section ===")
    r = c.post("/servers/111", data={"section": "nonsense", "csrf": token})
    assert r.status_code == 400, r.status_code
    print("  refused OK")

    print("\n=== greetings save, with the guards ===")
    r = c.post("/servers/111", data={
        "section": "welcome", "csrf": token, "welcome_enabled": "on",
        "welcome_channel": "900", "welcome_message": "Hi {user}", "welcome_embed": "on"})
    assert SAVED[111]["welcome_message"] == "Hi {user}"
    assert SAVED[111]["welcome_embed"] is True
    print("  welcome saved OK")

    r = c.post("/servers/111", data={
        "section": "welcome", "csrf": token, "welcome_enabled": "on",
        "welcome_channel": "900", "welcome_message": "   "})
    assert SAVED[111]["welcome_message"] is None
    assert SAVED[111]["welcome_enabled"] is False, "no wording means it can't be on"
    print("  an empty message switches it off rather than sending nothing OK")

    r = c.post("/servers/111", data={
        "section": "goodbye", "csrf": token, "goodbye_enabled": "on",
        "goodbye_message": "bye"})       # no channel
    assert SAVED[111]["goodbye_enabled"] is False, "goodbye needs a channel"
    print("  goodbye with no channel switches off OK")

    print("\n=== over-long text is truncated ===")
    r = c.post("/servers/111", data={
        "section": "welcome", "csrf": token, "welcome_enabled": "on",
        "welcome_channel": "900", "welcome_message": "x" * 5000})
    assert len(SAVED[111]["welcome_message"]) == store.MAX_TEXT
    print(f"  5000 chars cut to {store.MAX_TEXT} OK")

    print("\n=== unchecking a box turns it off ===")
    r = c.post("/servers/111", data={
        "section": "medialog", "csrf": token, "medialog_channel": "900"})
    assert SAVED[111]["medialog_enabled"] is False
    print("  an absent checkbox reads as off OK")

    print("\n=== the settings page renders ===")
    r = c.get("/servers/111")
    assert r.status_code == 200
    body = r.data.decode()
    for expected in ("Welcome message", "Deleted media", "Moderation log",
                     "Survey reminders", "#general", "#logs", "test-csrf"):
        assert expected in body, expected
    assert "{user}" in body, "the placeholder help should be shown"
    print("  all five cards, both channels and a csrf token present OK")

    print("\n=== a brand new server is told where to start ===")
    SAVED.pop(111, None)          # this suite's fake returns no role panels either
    body = c.get("/servers/111").data.decode()
    assert "Start here" in body, "a server with nothing on should be given a first step"
    assert "/setchannel" in body and "/logging setup" in body
    print("  the two commands that matter, and nothing else OK")

    # It has to disappear on its own, or it becomes furniture nobody reads.
    SAVED[111] = {"guild_id": 111, "welcome_enabled": True}
    body = c.get("/servers/111").data.decode()
    assert "Start here" not in body, "it should go once anything is switched on"
    print("  gone the moment one feature is on OK")
    SAVED.pop(111, None)

    print("\n=== the just-added page ===")
    body = c.get("/added?guild_id=111").data.decode()
    assert "/servers/111" in body, "it should link straight into that server"
    assert "/setchannel" in body and "/logging setup" in body
    # Reachable without one, since somebody may arrive from a link.
    plain = c.get("/added").data.decode()
    assert "/servers" in plain and "guild_id" not in plain
    # And a junk id must not end up in a link.
    assert "/servers/abc" not in c.get("/added?guild_id=abc").data.decode()
    print("  links into the server when it knows which, and ignores a junk id OK")

    print("\n=== the changelog ===")
    import changelog as log
    body = c.get("/changelog").data.decode()
    assert "What's new" in body
    for entry in log.ENTRIES:
        assert entry["title"] in body, entry["title"]
        for kind, _text in entry["changes"]:
            assert kind in log.KINDS, f"unknown kind {kind!r} in {entry['title']!r}"
    dates = [e["date"] for e in log.ENTRIES]
    assert dates == sorted(dates, reverse=True), f"newest first, got {dates}"
    total = sum(len(e["changes"]) for e in log.ENTRIES)
    print(f"  {len(log.ENTRIES)} entries, {total} changes, newest first OK")

    print("\n=== the docs page can be searched and linked into ===")
    import re as _re
    docs = c.get("/docs").data.decode()

    rows = docs.count("<tr>") - docs.count("<tr><th")
    command_rows = len(_re.findall(r'<td class="cmd">', docs))
    placeholder = _re.search(r'placeholder="Search (\d+) commands', docs)
    assert placeholder, "the search box should say how much it is searching"
    assert int(placeholder.group(1)) == command_rows, \
        (placeholder.group(1), command_rows)
    print(f"  search box offers all {command_rows} commands OK")

    # Every anchor has to point at an id that exists, or the permalink goes nowhere.
    ids = set(_re.findall(r'<(?:section|h3) id="([^"]+)"', docs))
    targets = _re.findall(r'class="anchor" href="#([^"]+)"', docs)
    assert targets, "headings should carry permalinks"
    missing = [t for t in targets if t not in ids]
    assert not missing, f"anchors pointing at nothing: {missing}"
    print(f"  {len(targets)} permalinks, all resolving OK")

    # And ids have to be unique, or the browser jumps to whichever came first.
    every_id = _re.findall(r' id="([^"]+)"', docs)
    dupes = {i for i in every_id if every_id.count(i) > 1}
    assert not dupes, f"duplicate ids would break linking: {sorted(dupes)}"
    print(f"  {len(every_id)} ids on the page, none repeated OK")

    print("\n=== logging out ===")
    r = c.post("/logout", data={"csrf": "test-csrf"})
    assert r.status_code == 302
    r = c.get("/servers")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    print("  session cleared OK")

    print("\n=== cookie hardening ===")
    cfg = dashboard.app.config
    assert cfg["SESSION_COOKIE_HTTPONLY"] is True
    assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax"
    print(f"  httponly, samesite={cfg['SESSION_COOKIE_SAMESITE']}, "
          f"max body {cfg['MAX_CONTENT_LENGTH']} bytes OK")

    print("\nALL CHECKS PASSED")


main()
