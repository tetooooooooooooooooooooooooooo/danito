"""Support tickets: opening, replying, closing, and the limits that stop it being a spam box.

A ticket is the one thing on the site where somebody types free text that a stranger will
read, so most of what matters here is refusals: another person's ticket, another person's
server, a closed thread, and one account opening fifty in a row.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")

import datetime, html, os, sys, types
sys.path.insert(0, WEB_DIR)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",
})

TICKETS = []
COUNTERS = {}


class FakeColl:
    def __init__(self, name): self.name = name
    def _rows(self):
        return TICKETS if self.name == "tickets" else []
    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict):
                if "$ne" in v and d.get(k) == v["$ne"]:
                    return False
            elif d.get(k) != v:
                return False
        return True
    def find(self, q=None, *a, **k):
        return _Cursor([d for d in self._rows() if self._match(d, q or {})])
    def find_one(self, q, *a, **k):
        if self.name == "runtime":
            return {"_id": "bot", "guild_ids": []}
        return next((d for d in self._rows() if self._match(d, q)), None)
    def count_documents(self, q):
        return len([d for d in self._rows() if self._match(d, q)])
    def insert_one(self, doc):
        # Mongo stamps an _id on the way in, and the update paths look it up by that.
        doc.setdefault("_id", len(TICKETS) + 1)
        TICKETS.append(doc)
        return types.SimpleNamespace(inserted_id=doc["_id"])
    def update_one(self, q, ops, upsert=False):
        hit = next((d for d in self._rows() if self._match(d, q)), None)
        if hit is None:
            return types.SimpleNamespace(matched_count=0)
        hit.update(ops.get("$set", {}))
        for field, value in ops.get("$push", {}).items():
            hit.setdefault(field, []).append(value)
        return types.SimpleNamespace(matched_count=1)
    def find_one_and_update(self, q, ops, upsert=False, return_document=None):
        key = q["_id"]
        for field, by in ops.get("$inc", {}).items():
            COUNTERS[key] = COUNTERS.get(key, 0) + by
        return {"_id": key, "seq": COUNTERS.get(key, 1)}


class _Cursor(list):
    def sort(self, key, direction=1):
        # Really sorts, because the cooldown asks for the newest ticket and a no-op here
        # would hand it the oldest instead.
        #
        # The two part key is so a missing value never gets compared against a real one:
        # Mongo sorts nulls first, Python raises. A document without the field is exactly
        # the case the cooldown has to survive, so the harness has to allow it.
        def rank(doc):
            value = doc.get(key)
            return (value is not None, value)
        return _Cursor(sorted(self, key=rank, reverse=direction < 0))
    def limit(self, n): return _Cursor(self[:n])


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

import discord_api as api
GUILDS = [{"id": "111", "name": "Mine", "icon": None, "owner": True, "permissions": "32"}]
api.manageable_guilds = lambda t, u, force=False: list(GUILDS)
api.guild_channels = lambda g: []
api.guild_roles = lambda g: []

import app as dashboard
dashboard.app.config["TESTING"] = True

ME, SOMEBODY_ELSE = 7, 8


def login(client, uid=ME):
    with client.session_transaction() as s:
        s["user"] = {"id": str(uid), "username": f"user{uid}", "avatar": ""}
        s["token"] = "user-token"
        s["csrf"] = "test-csrf"
    return "test-csrf"


def new(client, token, subject="It broke", body="Here is what happened", **extra):
    data = {"csrf": token, "category": "broken", "subject": subject, "body": body}
    data.update(extra)
    return client.post("/support/new", data=data)


def age_last(seconds):
    """Backdate the newest ticket so the cooldown isn't in the way."""
    TICKETS[-1]["created_at"] -= datetime.timedelta(seconds=seconds)


def main():
    c = dashboard.app.test_client()

    print("=== the page works logged out, and asks you to sign in ===")
    body = c.get("/support").data.decode()
    assert "Log in to open a ticket" in body
    assert "/login" in body
    # The self-serve links matter most to somebody who can't sign in.
    assert "Is the bot down?" in body and "#permissions" in body
    print("  self-serve links shown, ticket form behind a login OK")

    print("\n=== no invite yet, so it says so rather than linking nowhere ===")
    assert "Coming soon" in body and dashboard.PLACEHOLDER_INVITE not in body
    os.environ["SUPPORT_INVITE"] = "https://discord.gg/real"
    import importlib
    importlib.reload(dashboard)
    dashboard.app.config["TESTING"] = True
    c = dashboard.app.test_client()
    body = c.get("/support").data.decode()
    assert "https://discord.gg/real" in body and "Coming soon" not in body
    print("  placeholder hidden until a real invite is set OK")

    print("\n=== opening one ===")
    token = login(c)
    r = new(c, token, subject="Autorole does nothing", body="Nobody gets the role")
    assert r.status_code == 302 and "#ticket-1" in r.headers["Location"]
    assert len(TICKETS) == 1
    t = TICKETS[0]
    assert t["number"] == 1 and t["user_id"] == ME and t["status"] == "open"
    assert t["posted"] is False, "the bot has to be told to announce it"
    print(f"  ticket #{t['number']} open, waiting to be announced OK")

    print("\n=== a ticket needs something in it ===")
    age_last(120)
    before = len(TICKETS)
    new(c, token, subject="", body="no subject")
    new(c, token, subject="no body", body="   ")
    assert len(TICKETS) == before, "empty fields shouldn't create anything"
    print("  blank subject or body refused OK")

    print("\n=== attaching a server you don't administer ===")
    r = new(c, token, subject="About a server", body="...", guild_id="111")
    assert TICKETS[-1]["guild_id"] == 111, TICKETS[-1]
    age_last(120)
    r = new(c, token, subject="Somebody else's", body="...", guild_id="999")
    assert TICKETS[-1]["guild_id"] is None, "a server they don't manage must not attach"
    print("  their own server attaches, another's is dropped OK")

    print("\n=== the limits, one at a time ===")
    # Clear the decks: closed so the ceiling isn't in the way, and backdated because the
    # cooldown looks at the newest ticket whatever its status.
    for t in TICKETS:
        t["status"] = "closed"
        t["created_at"] -= datetime.timedelta(seconds=store.TICKET_COOLDOWN * 5)

    before = len(TICKETS)
    new(c, token, subject="First", body="...")
    assert len(TICKETS) == before + 1
    new(c, token, subject="Straight after", body="...")
    assert len(TICKETS) == before + 1, "the cooldown should have stopped the second"
    assert "seconds" in store.can_open_ticket(ME)
    print(f"  one every {store.TICKET_COOLDOWN}s OK")

    # Now fill up to the ceiling, backdating each so only the open count is in the way.
    while len([t for t in TICKETS if t["status"] != "closed"]) < store.MAX_OPEN_TICKETS:
        age_last(store.TICKET_COOLDOWN * 2)
        count = len(TICKETS)
        new(c, token, subject="Another", body="...")
        assert len(TICKETS) == count + 1, "should still be under the ceiling"

    age_last(store.TICKET_COOLDOWN * 2)
    before = len(TICKETS)
    new(c, token, subject="One too many", body="...")
    assert len(TICKETS) == before, f"{store.MAX_OPEN_TICKETS} open should be the ceiling"
    assert "already have" in store.can_open_ticket(ME)
    print(f"  and no more than {store.MAX_OPEN_TICKETS} open at once OK")

    print("\n=== a ticket with no timestamp doesn't take the page down ===")
    # store had two functions called _aware, one here and one under the insights, and the
    # later definition silently replaced the earlier one for every caller. They disagreed
    # about None, so a document missing created_at turned this check into "now - None".
    for t in TICKETS:
        t["status"] = "closed"
    TICKETS[0]["created_at"] = None
    TICKETS[0]["status"] = "open"
    blocked = store.can_open_ticket(ME)          # must not raise
    assert "seconds" not in blocked, blocked
    # And the page that calls it still renders rather than returning a 500.
    assert c.get("/support").status_code == 200
    print("  no crash, and an unaged ticket doesn't impose a cooldown OK")
    TICKETS[0]["created_at"] = datetime.datetime.now(datetime.timezone.utc)

    # Leave one open for the reply tests below.
    for t in TICKETS[1:]:
        t["status"] = "closed"
    TICKETS[0]["status"] = "open"

    print("\n=== replying, and what it does to the status ===")
    number = TICKETS[0]["number"]
    TICKETS[0]["status"] = "answered"
    r = c.post(f"/support/{number}/reply", data={"csrf": token, "body": "Still broken"})
    assert r.status_code == 302
    assert TICKETS[0]["status"] == "open", "their reply puts it back in the queue"
    assert TICKETS[0]["posted"] is False, "and the bot should announce it again"
    assert TICKETS[0]["messages"][-1] == {
        **TICKETS[0]["messages"][-1], "from": "you", "body": "Still broken"}
    print("  answered goes back to open, and is queued for announcing OK")

    print("\n=== somebody else's ticket ===")
    other = login(c, SOMEBODY_ELSE)
    r = c.post(f"/support/{number}/reply", data={"csrf": other, "body": "let me in"})
    assert not any(m["body"] == "let me in" for m in TICKETS[0]["messages"]), TICKETS[0]
    r = c.post(f"/support/{number}/close", data={"csrf": other})
    assert TICKETS[0]["status"] != "closed", "closing somebody else's must not work"
    assert store.ticket(SOMEBODY_ELSE, number) == {}
    body = c.get("/support").data.decode()
    assert "Autorole does nothing" not in body, "and it must not be listed for them"
    print("  can't read, reply to, or close a ticket that isn't theirs OK")

    print("\n=== closing your own, and what happens after ===")
    token = login(c, ME)
    c.post(f"/support/{number}/close", data={"csrf": token})
    assert TICKETS[0]["status"] == "closed"
    c.post(f"/support/{number}/reply", data={"csrf": token, "body": "one more thing"})
    assert not any(m["body"] == "one more thing" for m in TICKETS[0]["messages"]), \
        "a closed ticket takes no more replies"
    print("  closed, and stays closed OK")

    print("\n=== forged posts ===")
    before = len(TICKETS)
    for data in ({"csrf": "wrong", "subject": "x", "body": "y"}, {"subject": "x", "body": "y"}):
        r = c.post("/support/new", data=data)
        assert r.status_code == 400, (data, r.status_code)
    assert len(TICKETS) == before
    print("  wrong and missing tokens both refused OK")

    print("\n=== the thread renders, and is escaped ===")
    TICKETS.clear(); COUNTERS.clear()
    store.open_ticket(ME, "user7", "broken", "Tags <script>alert(1)</script>",
                      "Body with <b>markup</b> & an ampersand")
    TICKETS[0]["messages"] = [{"from": "staff", "author": "tet", "body": "Looking now",
                               "at": datetime.datetime.now(datetime.timezone.utc)}]
    TICKETS[0]["status"] = "answered"
    body = c.get("/support").data.decode()
    assert "<script>alert(1)</script>" not in body, "what somebody types must not be markup"
    assert "&lt;script&gt;" in body
    assert "Looking now" in body and "Replied" in body
    assert 'id="ticket-1"' in body
    print("  staff reply shown, typed markup escaped OK")

    print("\n=== the bot's categories match the website's ===")
    sys.path.insert(0, SRC_DIR)
    src = (ROOT / "src" / "Cogs" / "Support.py").read_text(encoding="utf-8")
    for key, label in store.TICKET_CATEGORIES:
        assert f'"{key}"' in src, f"the bot doesn't know the {key} category"
        assert label in src, f"the bot labels {key} differently"
    print(f"  {len(store.TICKET_CATEGORIES)} categories, worded the same in both OK")

    print("\n=== support is linked from every page ===")
    # follow_redirects because / bounces a signed-in visitor to the server picker.
    for path in ("/", "/docs", "/status", "/support"):
        assert "/support" in c.get(path, follow_redirects=True).data.decode(), path
    print("  in the header and the footer OK")

    print("\nALL CHECKS PASSED")


main()
