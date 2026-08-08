"""The dashboard's logging card.

Twelve events, each with a toggle and an optional channel of its own. The rules worth holding
onto are that a channel from another server never gets written, that leaving the dropdown on
"Same as above" stores nothing rather than a guess, and that logging cannot be left switched on
with nowhere at all for the entries to go.
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
    "DASHBOARD_INSECURE_COOKIES": "1",
})

SAVED = {}
DIRTY = set()


class Cursor(list):
    def limit(s, n): return Cursor(s[:n])
    def sort(s, *a, **k): return s


class FakeColl:
    def __init__(self, name): self.name = name
    def find(self, q=None, *a, **k): return Cursor()
    def count_documents(self, q): return 0
    def find_one(self, q, *a, **k):
        if self.name == "runtime":
            return {"_id": "bot", "guild_ids": [111]}
        return SAVED.get(q.get("guild_id"))
    def update_one(self, q, ops, upsert=False):
        if self.name == "config_dirty":
            DIRTY.add(q["_id"]); return types.SimpleNamespace(matched_count=1)
        gid = q["guild_id"]
        SAVED.setdefault(gid, {"guild_id": gid}).update(ops.get("$set", {}))
        return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

import discord_api as api

api.manageable_guilds = lambda token, uid, force=False: [
    {"id": "111", "name": "Managed", "icon": None, "owner": False, "permissions": str(0x20)}]
api.guild_channels = lambda gid: ([
    {"id": "900", "name": "server-log", "type": 0, "position": 0},
    {"id": "901", "name": "join-log", "type": 0, "position": 1},
] if gid == 111 else [])
api.guild_roles = lambda gid: []

import app as dashboard
dashboard.app.config["TESTING"] = True

KEYS = store.LOG_EVENT_KEYS


def login(client):
    with client.session_transaction() as s:
        s["user"] = {"id": "7", "username": "Admin", "avatar": ""}
        s["token"] = "user-token"
        s["csrf"] = "test-csrf"
    return "test-csrf"


def form(token, main="900", on=(), channels=None):
    """Build a submission the way the page would."""
    data = {"section": "logging", "csrf": token, "logging_enabled": "on"}
    if main is not None:
        data["log_channel"] = main
    for key in on:
        data[f"log_on_{key}"] = "on"
    for key, cid in (channels or {}).items():
        data[f"log_ch_{key}"] = cid
    return data


def main():
    c = dashboard.app.test_client()
    token = login(c)

    print("=== everything on, one channel ===")
    r = c.post("/servers/111", data=form(token, on=KEYS))
    assert r.status_code == 302 and r.headers["Location"].endswith("#logging")
    saved = SAVED[111]
    assert saved["logging_enabled"] is True
    assert saved["log_channel"] == 900
    assert set(saved["log_events"]) == set(KEYS), saved["log_events"].keys()
    assert all(e["on"] for e in saved["log_events"].values())
    assert all(e["channel"] is None for e in saved["log_events"].values())
    assert 111 in DIRTY
    print(f"  {len(KEYS)} events stored, all on, all sharing #server-log OK")

    print("\n=== one event pointed somewhere of its own ===")
    c.post("/servers/111", data=form(token, on=KEYS, channels={"member_join": "901"}))
    events = SAVED[111]["log_events"]
    assert events["member_join"]["channel"] == 901, events["member_join"]
    assert events["member_leave"]["channel"] is None
    print("  member_join to #join-log, the rest still shared OK")

    print("\n=== 'Same as above' stores nothing, not a guess ===")
    c.post("/servers/111", data=form(token, on=KEYS, channels={"member_join": ""}))
    assert SAVED[111]["log_events"]["member_join"]["channel"] is None
    print("  blank means fall back OK")

    print("\n=== a channel from another server is refused ===")
    c.post("/servers/111", data=form(token, on=KEYS,
                                     channels={"member_ban": "999999", "member_join": "abc"}))
    events = SAVED[111]["log_events"]
    assert events["member_ban"]["channel"] is None, events["member_ban"]
    assert events["member_join"]["channel"] is None, events["member_join"]
    assert SAVED[111]["logging_enabled"] is True, "it still has the shared channel"
    print("  unknown id and junk both discarded, falling back to the shared channel OK")

    print("\n=== unticked events are switched off ===")
    keep = ["message_delete", "member_ban"]
    c.post("/servers/111", data=form(token, on=keep))
    events = SAVED[111]["log_events"]
    assert [k for k, v in events.items() if v["on"]] == keep, events
    assert set(events) == set(KEYS), "every key is written, not just the ticked ones"
    print(f"  2 on, {len(KEYS) - 2} off, all {len(KEYS)} keys present OK")

    print("\n=== an event dropped from the form does not linger ===")
    c.post("/servers/111", data=form(token, on=[]))
    assert not any(e["on"] for e in SAVED[111]["log_events"].values())
    print("  saving with nothing ticked switches them all off OK")

    print("\n=== it can't be on with nowhere to go ===")
    c.post("/servers/111", data=form(token, main="", on=KEYS))
    assert SAVED[111]["logging_enabled"] is False, SAVED[111]
    assert SAVED[111]["log_channel"] is None
    print("  no shared channel and no per-event channel: switched off OK")

    c.post("/servers/111", data=form(token, main="", on=["member_join"],
                                     channels={"member_join": "901"}))
    assert SAVED[111]["logging_enabled"] is True, "one event has its own channel, so it works"
    assert SAVED[111]["log_channel"] is None
    print("  no shared channel but one event has its own: stays on OK")

    c.post("/servers/111", data=form(token, main="", on=["member_join"],
                                     channels={"member_leave": "901"}))
    assert SAVED[111]["logging_enabled"] is False, \
        "the channel belongs to an event that is switched off"
    print("  a channel on a switched-off event doesn't count OK")

    print("\n=== the master toggle ===")
    data = form(token, on=KEYS)
    del data["logging_enabled"]
    c.post("/servers/111", data=data)
    assert SAVED[111]["logging_enabled"] is False
    assert any(e["on"] for e in SAVED[111]["log_events"].values()), \
        "the per-event choices are kept so switching back on restores them"
    print("  unticked master switch turns it off but keeps the choices OK")

    print("\n=== forged and unauthorised posts ===")
    before = dict(SAVED[111])
    r = c.post("/servers/111", data={"section": "logging", "csrf": "wrong"})
    assert r.status_code == 400, r.status_code
    assert SAVED[111] == before, "a bad token must not write"
    r = c.post("/servers/333", data=form(token, on=KEYS))
    assert r.status_code == 404, r.status_code
    print("  400 and 404, nothing written OK")

    print("\n=== the card renders ===")
    r = c.get("/servers/111")
    body = r.data.decode()
    assert r.status_code == 200
    for key, icon, label, blurb in store.LOG_EVENTS:
        assert f'name="log_on_{key}"' in body, key
        assert f'name="log_ch_{key}"' in body, key
        assert label in body, label
    assert body.count("Same as above") == len(KEYS), "every row offers the shared channel"
    assert 'value="logging"' in body and "Send everything to" in body
    assert "All on" in body and "All off" in body
    print(f"  {len(KEYS)} rows, each with a toggle, a dropdown and its explanation OK")

    print("\nALL CHECKS PASSED")


main()
