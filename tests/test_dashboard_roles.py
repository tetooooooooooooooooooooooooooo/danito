"""The dashboard's role settings: autorole and the role button panels.

The point of interest is that the dashboard cannot see Discord's role hierarchy the way the bot
can, so it has to ask. A role the bot could never assign must be refused at save time, not
accepted and then quietly ignored forever.
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

from bson import ObjectId

SAVED = {}
DIRTY = set()
PANELS = []


class Cursor(list):
    def limit(self, n): return Cursor(self[:n])
    def sort(self, *a, **k): return self


class FakeColl:
    def __init__(self, name): self.name = name
    def _match(self, d, q): return all(d.get(k) == v for k, v in q.items())
    def find_one(self, q, *a, **k):
        if self.name == "runtime":
            return {"_id": "bot", "guild_ids": [111]}
        if self.name == "role_panels":
            return next((dict(d) for d in PANELS if self._match(d, q)), None)
        return SAVED.get(q.get("guild_id"))
    def find(self, q=None, *a, **k):
        return Cursor([dict(d) for d in PANELS if self._match(d, q or {})])
    def count_documents(self, q):
        return len([d for d in PANELS if self._match(d, q)])
    def insert_one(self, doc):
        doc.setdefault("_id", ObjectId()); PANELS.append(doc)
        return types.SimpleNamespace(inserted_id=doc["_id"])
    def update_one(self, q, ops, upsert=False):
        if self.name == "config_dirty":
            DIRTY.add(q["_id"]); return types.SimpleNamespace(matched_count=1)
        if self.name == "role_panels":
            hit = next((d for d in PANELS if self._match(d, q)), None)
            if hit is None:
                return types.SimpleNamespace(matched_count=0)
            hit.update(ops.get("$set", {}))
            return types.SimpleNamespace(matched_count=1)
        gid = q["guild_id"]
        SAVED.setdefault(gid, {"guild_id": gid}).update(ops.get("$set", {}))
        return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

import discord_api as api

USER_GUILDS = [{"id": "111", "name": "Managed", "icon": None,
                "owner": False, "permissions": str(0x20)}]
CHANNELS = [{"id": "900", "name": "general", "type": 0, "position": 0},
            {"id": "901", "name": "roles", "type": 0, "position": 1}]
# 10 and 11 are fine; 12 is above the bot; 13 belongs to an integration.
ROLES = [
    {"id": "10", "name": "Red", "position": 5, "problem": None, "colour": "#ff0000"},
    {"id": "11", "name": "Blue", "position": 6, "problem": None, "colour": ""},
    {"id": "12", "name": "Admin", "position": 80,
     "problem": "sits above my highest role, so I can't hand it out", "colour": ""},
    {"id": "13", "name": "Booster", "position": 4,
     "problem": "managed by an integration, so Discord won't let anyone assign it",
     "colour": ""},
]

api.manageable_guilds = lambda token, uid, force=False: list(USER_GUILDS)
api.guild_channels = lambda gid: list(CHANNELS) if gid == 111 else []
api.guild_roles = lambda gid: [dict(r) for r in ROLES] if gid == 111 else []

import app as dashboard
dashboard.app.config["TESTING"] = True


def login(client):
    with client.session_transaction() as s:
        s["user"] = {"id": "7", "username": "Admin", "avatar": ""}
        s["token"] = "user-token"
    with client.session_transaction() as s:
        s["csrf"] = "test-csrf"
    return "test-csrf"


def main():
    c = dashboard.app.test_client()
    token = login(c)

    print("=== autorole saves only roles the bot could actually give ===")
    r = c.post("/servers/111", data={
        "section": "autorole", "csrf": token, "autorole_enabled": "on",
        "autorole_ids": ["10", "11", "12", "13", "999"]})
    assert r.status_code == 302, r.status_code
    assert SAVED[111]["autorole_ids"] == [10, 11], SAVED[111]["autorole_ids"]
    assert SAVED[111]["autorole_enabled"] is True
    assert 111 in DIRTY
    print(f"  kept {SAVED[111]['autorole_ids']}, dropped the too-high, the managed "
          f"and the unknown OK")

    print("\n=== nothing to hand out means it can't be on ===")
    c.post("/servers/111", data={
        "section": "autorole", "csrf": token, "autorole_enabled": "on",
        "autorole_ids": ["12"]})
    assert SAVED[111]["autorole_ids"] == []
    assert SAVED[111]["autorole_enabled"] is False
    print("  switched off rather than left on with an empty list OK")

    print("\n=== the autorole limit is enforced on the way in ===")
    api.guild_roles = lambda gid: [
        {"id": str(i), "name": f"r{i}", "position": 1, "problem": None, "colour": ""}
        for i in range(20, 40)]
    c.post("/servers/111", data={
        "section": "autorole", "csrf": token, "autorole_enabled": "on",
        "autorole_ids": [str(i) for i in range(20, 40)]})
    assert len(SAVED[111]["autorole_ids"]) == store.MAX_AUTOROLES
    print(f"  20 submitted, {store.MAX_AUTOROLES} stored OK")
    api.guild_roles = lambda gid: [dict(r) for r in ROLES] if gid == 111 else []

    print("\n=== creating a panel ===")
    r = c.post("/servers/111/panels", data={
        "csrf": token, "title": "Colours", "description": "Pick one",
        "channel_id": "901", "mode": "single"})
    assert r.status_code == 302, r.status_code
    assert len(PANELS) == 1, PANELS
    p = PANELS[0]
    assert p["title"] == "Colours" and p["mode"] == "single" and p["channel_id"] == 901
    assert p["needs_publish"] is False, "an empty panel has nothing to post"
    print(f"  created {p['title']!r} in #roles, not yet queued OK")

    print("\n=== a channel from somewhere else is refused ===")
    r = c.post("/servers/111/panels", data={
        "csrf": token, "title": "Bad", "channel_id": "999999", "mode": "toggle"})
    assert r.status_code == 400, r.status_code
    assert len(PANELS) == 1, "nothing should have been created"
    print("  400, nothing created OK")

    print("\n=== saving a panel builds the buttons ===")
    pid = str(PANELS[0]["_id"])
    r = c.post(f"/servers/111/panels/{pid}", data={
        "csrf": token, "title": "Colours", "description": "Pick one",
        "channel_id": "901", "mode": "toggle",
        "role_ids": ["10", "11"],
        "label_10": "I like red", "emoji_10": "🔴",
        "label_11": "", "emoji_11": ""})
    assert r.status_code == 302, r.status_code
    p = PANELS[0]
    assert p["roles"] == [
        {"role_id": 10, "label": "I like red", "emoji": "🔴"},
        {"role_id": 11, "label": "Blue", "emoji": None},
    ], p["roles"]
    assert p["needs_publish"] is True, "the bot has to be told to post it"
    assert p["publish_error"] is None
    print("  custom label kept, blank label fell back to the role name OK")

    print("\n=== a role the bot can't assign never reaches a panel ===")
    c.post(f"/servers/111/panels/{pid}", data={
        "csrf": token, "title": "Colours", "channel_id": "901", "mode": "toggle",
        "role_ids": ["10", "12", "13"], "label_12": "sneaky"})
    assert [r["role_id"] for r in PANELS[0]["roles"]] == [10], PANELS[0]["roles"]
    print("  Admin and Booster were dropped OK")

    print("\n=== the same role twice becomes one button ===")
    c.post(f"/servers/111/panels/{pid}", data={
        "csrf": token, "title": "Colours", "channel_id": "901", "mode": "toggle",
        "role_ids": ["10", "10", "11"]})
    assert [r["role_id"] for r in PANELS[0]["roles"]] == [10, 11]
    print("  deduplicated OK")

    print("\n=== a panel id belonging to another server ===")
    other = {"_id": ObjectId(), "guild_id": 999, "channel_id": 901, "title": "Theirs",
             "roles": [], "mode": "toggle", "needs_publish": False}
    PANELS.append(other)
    r = c.post(f"/servers/111/panels/{other['_id']}", data={
        "csrf": token, "title": "Stolen", "channel_id": "901", "mode": "toggle"})
    assert r.status_code == 404, r.status_code
    assert other["title"] == "Theirs", "the other server's panel must be untouched"
    print("  404, untouched OK")

    r = c.post(f"/servers/111/panels/{other['_id']}", data={"csrf": token, "delete": "1"})
    assert other.get("pending_delete") is None, other
    print("  and it can't be deleted either OK")

    print("\n=== a malformed panel id ===")
    r = c.post("/servers/111/panels/not-an-objectid", data={
        "csrf": token, "title": "x", "channel_id": "901", "mode": "toggle"})
    assert r.status_code == 404, r.status_code
    print("  404 rather than a 500 OK")

    print("\n=== deleting is queued for the bot, not done here ===")
    r = c.post(f"/servers/111/panels/{pid}", data={"csrf": token, "delete": "1"})
    assert r.status_code == 302
    assert PANELS[0]["pending_delete"] is True
    assert PANELS[0]["needs_publish"] is False
    print("  flagged, so the bot can remove the Discord message too OK")

    print("\n=== forged posts are refused ===")
    before = len(PANELS)
    for data in ({"csrf": "wrong", "title": "x", "channel_id": "901", "mode": "toggle"},
                 {"title": "x", "channel_id": "901", "mode": "toggle"}):
        r = c.post("/servers/111/panels", data=data)
        assert r.status_code == 400, (data, r.status_code)
    assert len(PANELS) == before
    print("  wrong and missing tokens both 400, nothing created OK")

    print("\n=== a server they don't administer ===")
    for path in ("/servers/333/panels", f"/servers/333/panels/{pid}"):
        r = c.post(path, data={"csrf": token, "title": "x",
                               "channel_id": "901", "mode": "toggle"})
        assert r.status_code == 404, (path, r.status_code)
    print("  both panel routes 404 OK")

    print("\n=== the settings page renders the new cards ===")
    r = c.get("/servers/111")
    assert r.status_code == 200, r.status_code
    body = r.data.decode()
    for expected in ("Autorole", "Role buttons", "Red", "Blue", "Admin",
                     "sits above my highest role", "#ff0000", "Add a panel"):
        assert expected in body, expected
    # The unusable ones are shown but not selectable, so nobody hunts for a missing role.
    assert body.count("disabled") >= 2, "unassignable roles should be disabled, not hidden"
    print("  both cards, every role, the colour swatch and the reasons are present OK")

    print("\n=== the sections are tabs, and work without JavaScript ===")
    import re
    pane_ids = re.findall(r'<section class="pane" id="([a-z]+)"', body)
    tab_ids = re.findall(r'data-tab="([a-z]+)"', body)
    assert pane_ids == tab_ids, (pane_ids, tab_ids)
    for expected in ("welcome", "goodbye", "autorole", "panels", "automod", "logging",
                     "survey"):
        assert expected in pane_ids, expected
    assert len(pane_ids) == 7, pane_ids
    # All four logs share the one tab now, so three sections have no pane of their own. They
    # still have to be reachable, and saving one has to come back to the tab it lives in.
    for section in ("modlog", "medialog", "pinglog"):
        assert section not in pane_ids, f"{section} should share the logging tab"
        assert f'value="{section}"' in body, f"the {section} form should be inside a pane"
        r2 = c.post("/servers/111", data={"section": section, "csrf": token})
        assert r2.headers["Location"].endswith("#logging"), (section, r2.headers["Location"])
    print("  6 tabs, all four logs in one of them, each saving back to it OK")
    # Every pane is in the markup and none is hidden server side, so with JavaScript off the
    # page is the plain list it used to be rather than a single tab with no way to leave it.
    # The attribute on a tag, not the word: page copy is allowed to say "hidden".
    assert not re.search(r"<[^>]*\shidden(\s|>|=)", body), \
        "panes must only ever be hidden by the script"
    print(f"  {len(pane_ids)} panes, {len(tab_ids)} tabs, nothing hidden server side OK")

    print("\n=== a save comes back to the tab you were on ===")
    r = c.post("/servers/111", data={
        "section": "autorole", "csrf": token, "autorole_ids": ["10"], "autorole_enabled": "on"})
    assert r.headers["Location"].endswith("#autorole"), r.headers["Location"]
    r = c.post("/servers/111/panels", data={
        "csrf": token, "title": "Another", "channel_id": "900", "mode": "toggle"})
    assert r.headers["Location"].endswith("#panels"), r.headers["Location"]
    print("  both redirects carry the section anchor OK")

    print("\nALL CHECKS PASSED")


main()
