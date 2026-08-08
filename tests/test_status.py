"""The status page.

The bot and the dashboard are separate processes that share only Mongo, so "is it up" is
really "how long since the bot last wrote down that it was". The thresholds are the whole
feature: too tight and every deploy reads as an outage, too loose and a real one goes unnoticed.

It also has to answer when the database itself is unreachable, because that is exactly the
moment somebody loads it.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")

import datetime, html, json, os, sys, types
sys.path.insert(0, WEB_DIR)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",
})

RUNTIME = {}
BROKEN = False


class FakeColl:
    def __init__(self, name): self.name = name
    def find_one(self, q, *a, **k):
        if BROKEN:
            raise RuntimeError("no route to host")
        return dict(RUNTIME) if self.name == "runtime" and RUNTIME else None
    def find(self, q=None, *a, **k): return []
    def update_one(self, *a, **k): return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

import discord_api as api
api.manageable_guilds = lambda t, u, force=False: []
api.guild_channels = lambda g: []
api.guild_roles = lambda g: []

import app as dashboard
dashboard.app.config["TESTING"] = True


def beat(seconds_ago, **extra):
    """Pretend the bot last checked in this long ago."""
    global BROKEN
    BROKEN = False
    now = datetime.datetime.now(datetime.timezone.utc)
    last = now - datetime.timedelta(seconds=seconds_ago)
    RUNTIME.clear()
    RUNTIME.update({
        "_id": "bot",
        "last_seen": last,
        # Uptime is measured to the last heartbeat, not to now, so pin it there or the
        # expected figure drifts with how stale the beat is.
        "started_at": last - datetime.timedelta(hours=5),
        "guild_count": 42, "member_count": 97690, "latency_ms": 63,
        **extra,
    })


def main():
    c = dashboard.app.test_client()

    print("=== the thresholds ===")
    for seconds, expected in ((0, "up"), (60, "up"), (store.HEARTBEAT_GRACE, "up"),
                              (store.HEARTBEAT_GRACE + 1, "wobbly"),
                              (store.HEARTBEAT_DOWN, "wobbly"),
                              (store.HEARTBEAT_DOWN + 1, "down"),
                              (86400, "down")):
        beat(seconds)
        got = store.bot_status()["state"]
        assert got == expected, (seconds, got, expected)
        print(f"  quiet for {seconds:>6}s -> {got}")
    print("  a restart reads as a wobble, a real outage as down OK")

    print("\n=== the page says which, in words as well as colour ===")
    beat(10)
    body = html.unescape(c.get("/status").data.decode())
    assert 'class="status-card up"' in body
    assert "All good" in body and "online and answering" in body
    assert "97,690" not in body, "member count isn't on the page"
    assert "42" in body, "the server count is"
    assert "5 hours" in body, "and how long it has been running"
    print("  up: heading, detail, servers and uptime all present OK")

    beat(store.HEARTBEAT_DOWN + 60)
    body = html.unescape(c.get("/status").data.decode())
    assert 'class="status-card down"' in body
    assert "Offline" in body and "commands won't be working" in body
    print("  down: says so plainly OK")

    print("\n=== it works before the bot has ever checked in ===")
    RUNTIME.clear()
    status = store.bot_status()
    assert status["state"] == "unknown", status
    body = html.unescape(c.get("/status").data.decode())
    assert "Not sure" in body and "hasn't checked in" in body
    print("  no heartbeat yet: says it doesn't know rather than guessing OK")

    print("\n=== and when the database is the thing that's down ===")
    global BROKEN
    BROKEN = True
    status = store.bot_status()
    assert status["state"] == "unknown", status
    assert "database" in status["reason"], status
    r = c.get("/status")
    assert r.status_code == 200, "the one page that must not 500 when Mongo is unreachable"
    assert "can't reach the database" in html.unescape(r.data.decode())
    print("  answers rather than throwing, and says why OK")
    BROKEN = False

    print("\n=== a naive timestamp from Mongo doesn't crash the comparison ===")
    # pymongo hands back naive UTC datetimes, and comparing one to an aware now raises.
    beat(30)
    RUNTIME["last_seen"] = RUNTIME["last_seen"].replace(tzinfo=None)
    RUNTIME["started_at"] = RUNTIME["started_at"].replace(tzinfo=None)
    assert store.bot_status()["state"] == "up"
    print("  handled OK")

    print("\n=== the json endpoint ===")
    beat(45)
    r = c.get("/status.json")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["state"] == "up" and data["guilds"] == 42
    assert data["heading"] == "All good"
    assert data["dashboard"] == "up", "you are reading it, so it is up"
    # Datetimes have to survive the trip.
    assert isinstance(data["last_seen"], str) and "T" in data["last_seen"]
    json.dumps(data)          # must not raise
    print(f"  {data['state']}, {data['quiet_for']} quiet, {data['uptime']} uptime OK")

    RUNTIME.clear()
    data = json.loads(c.get("/status.json").data)
    assert data["state"] == "unknown" and data["last_seen"] is None
    json.dumps(data)
    print("  and stays valid json with nothing to report OK")

    print("\n=== it needs no login ===")
    beat(10)
    for path in ("/status", "/status.json"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
    # Somebody whose bot is down should not meet a sign-in form.
    assert "/login" not in c.get("/status").headers.get("Location", "")
    print("  both reachable while logged out OK")

    print("\n=== it's linked from every page ===")
    for path in ("/", "/docs", "/status"):
        assert '/status"' in c.get(path).data.decode(), path
    print("  in the header on the landing page, the docs and itself OK")

    print("\n=== so are the terms and the privacy policy ===")
    # Discord wants both reachable from anywhere the bot is offered, and somebody deciding
    # whether to add it shouldn't have to sign in to read what it does with their data.
    for path in ("/", "/docs", "/status"):
        body = c.get(path).data.decode()
        assert dashboard.TERMS_URL in body, path
        assert dashboard.PRIVACY_URL in body, path
        assert "Terms of Service" in body and "Privacy Policy" in body, path
    assert dashboard.TERMS_URL.startswith("https://"), dashboard.TERMS_URL
    assert dashboard.PRIVACY_URL.startswith("https://"), dashboard.PRIVACY_URL
    print("  both in the footer of every public page, while logged out OK")

    print("\n=== the wording covers every state ===")
    assert set(dashboard.STATUS_WORDS) == {"up", "wobbly", "down", "unknown"}
    for state, (heading, detail) in dashboard.STATUS_WORDS.items():
        assert heading and detail, state
    print(f"  {len(dashboard.STATUS_WORDS)} states, all with something to say OK")

    print("\n=== how long ago, in words ===")
    for seconds, expected in ((0, "0 seconds"), (1, "1 second"), (59, "59 seconds"),
                              (60, "1 minute"), (119, "1 minute"), (120, "2 minutes"),
                              (3600, "1 hour"), (86400, "1 day"), (172800, "2 days")):
        got = dashboard._ago(seconds)
        assert got == expected, (seconds, got, expected)
    assert dashboard._ago(None) == "unknown"
    print("  singular and plural both right OK")

    print("\nALL CHECKS PASSED")


main()
