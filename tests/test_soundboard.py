"""The soundboard dashboard, and the one calculation in it that has to be right.

Discord gives a soundboard sound no position field. The order it returns them in is the order
members see, and it comes from when each was uploaded, so the only way to reorder is to delete
and recreate. That makes reordering destructive in a way renaming is not: a recreated sound has
a new id, and a new id is gone from every member's favourites.

`plan_reorder` is what decides how much of that damage a given drag costs, and the number it
returns is what the confirmation shows somebody before they commit. Getting it wrong either
understates the damage or rebuilds sounds that did not need rebuilding, so it is tested here
against a fake Discord rather than left to be discovered on a real server.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
WEB_DIR = str(ROOT / "web")

import html, os, sys, types
sys.path.insert(0, WEB_DIR)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",
})

GUILD = 17


class FakeColl:
    def __init__(self, name): self.name = name
    def find_one(self, *a, **k): return None
    def find(self, *a, **k): return []
    def update_one(self, *a, **k): return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()
# require_guild checks this as well as the user's own permissions: being an admin of a server
# the bot is not in should not open a settings page for it.
store.bot_guild_ids = lambda: {GUILD}

import discord_api as api
api.manageable_guilds = lambda t, u, force=False: [
    {"id": str(GUILD), "name": "Cool Server", "icon": None}]
api.guild_channels = lambda g: []
api.guild_roles = lambda g: []

import app as dashboard
dashboard.app.config["TESTING"] = True


def sound(sid, name, volume=1.0, emoji=None):
    return {"sound_id": str(sid), "name": name, "volume": volume, "emoji_name": emoji,
            "emoji_id": None, "available": True}


def logged_in(client):
    with client.session_transaction() as s:
        s["user"] = {"id": "99", "username": "admin"}
        s["token"] = "user-token"
        s["csrf"] = "test-csrf"
    return client


def main():
    c = logged_in(dashboard.app.test_client())
    board = [sound(1, "airhorn"), sound(2, "bruh"), sound(3, "clap"), sound(4, "drum")]

    print("=== reordering costs everything from the first change down ===")
    plan = dashboard.plan_reorder
    ids = lambda plan_out: [s["sound_id"] for s in plan_out]

    # Swapping the last two touches those two and nothing above them.
    moving, problem = plan(board, ["1", "2", "4", "3"])
    assert not problem, problem
    assert ids(moving) == ["4", "3"], ids(moving)
    print(f"  swap the last two -> rebuild {ids(moving)}")

    # Moving the first to the end touches every single one, which is the case worth warning
    # loudest about.
    moving, problem = plan(board, ["2", "3", "4", "1"])
    assert ids(moving) == ["2", "3", "4", "1"], ids(moving)
    print(f"  move the first to the end -> rebuild all {len(moving)}")

    # Moving the last to the front, likewise.
    moving, _ = plan(board, ["4", "1", "2", "3"])
    assert ids(moving) == ["4", "1", "2", "3"]
    print("  move the last to the front -> rebuild all 4")

    print("\n=== and nothing at all when nothing moved ===")
    moving, problem = plan(board, ["1", "2", "3", "4"])
    assert moving == [] and "already the order" in problem, (moving, problem)
    moving, problem = plan(board, [])
    assert moving == [] and "Nothing to reorder" in problem
    print("  an unchanged order is refused rather than rebuilt OK")

    print("\n=== a board that changed underneath you is refused ===")
    # Somebody uploading or deleting in Discord while the page was open. Rebuilding against a
    # stale list would delete a sound that is not in the new order and never put it back.
    for wanted, why in ((["1", "2", "3"], "one went missing"),
                        (["1", "2", "3", "4", "5"], "one appeared"),
                        (["1", "2", "3", "9"], "an id nobody recognises")):
        moving, problem = plan(board, wanted)
        assert moving == [] and "changed while you were arranging it" in problem, (wanted, problem)
        print(f"  {why}: refused")

    print("\n=== the page lists what is there ===")
    api.guild_sounds = lambda g: board
    body = html.unescape(c.get(f"/servers/{GUILD}/soundboard").data.decode())
    for s in board:
        assert s["name"] in body, s["name"]
    assert "4 sounds" in body
    # The distinction the whole page turns on has to be on the page, not just in the modal.
    assert "stays in everybody's favourites" in body
    assert "drops them out of every member's favourites" in body
    print("  all four sounds, and the warning about what reordering costs OK")

    print("\n=== renaming does not touch the audio ===")
    calls = []
    api.edit_sound = lambda g, sid, **kw: calls.append((g, sid, kw))
    api.create_sound = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a rename must never re-upload"))
    api.delete_sound = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a rename must never delete"))
    r = c.post(f"/servers/{GUILD}/soundboard/1",
               data={"csrf": "test-csrf", "name": "airhorn 2", "volume": "0.5", "emoji": "📢"})
    assert r.status_code == 302
    assert len(calls) == 1, calls
    guild_id, sid, fields = calls[0]
    assert guild_id == GUILD and sid == 1, calls[0]
    assert fields["name"] == "airhorn 2" and fields["volume"] == 0.5
    assert fields["emoji_name"] == "📢", fields
    print(f"  edit_sound({fields}) and nothing else OK")

    print("\n=== a name Discord would refuse never leaves the dashboard ===")
    calls.clear()
    for bad in ("", "x", "y" * 40):
        r = c.post(f"/servers/{GUILD}/soundboard/1",
                   data={"csrf": "test-csrf", "name": bad, "volume": "1"})
        assert r.status_code == 302
    assert not calls, "none of those should have reached Discord"
    print("  too short and too long both refused locally OK")

    print("\n=== volume is clamped rather than passed through ===")
    calls.clear()
    for sent, want in (("2", 1.0), ("-1", 0.0), ("nonsense", 1.0), ("0.25", 0.25)):
        c.post(f"/servers/{GUILD}/soundboard/1",
               data={"csrf": "test-csrf", "name": "ok", "volume": sent})
    got = [f["volume"] for _, _, f in calls]
    assert got == [1.0, 0.0, 1.0, 0.25], got
    print(f"  {got} OK")

    print("\n=== every write needs the csrf token ===")
    for path, data in (
        (f"/servers/{GUILD}/soundboard/upload", {}),
        (f"/servers/{GUILD}/soundboard/1", {"name": "x"}),
        (f"/servers/{GUILD}/soundboard/1/delete", {}),
        (f"/servers/{GUILD}/soundboard/reorder", {"order": "1,2"}),
    ):
        r = c.post(path, data=data)
        assert r.status_code == 400, (path, r.status_code)
    print("  upload, edit, delete and reorder all refuse a post without one OK")

    print("\n=== and a guild you don't administer is a 404 ===")
    for path in (f"/servers/999/soundboard", f"/servers/999/soundboard/reorder"):
        r = c.post(path, data={"csrf": "test-csrf"}) if path.endswith("reorder") \
            else c.get(path)
        assert r.status_code == 404, (path, r.status_code)
    print("  same answer whether it exists or not OK")

    print("\n=== an upload big enough to matter is allowed through the app ===")
    # The cap was 64KB before this page existed, which is smaller than a single sound.
    assert dashboard.app.config["MAX_CONTENT_LENGTH"] >= api.SOUND_MAX_BYTES, \
        dashboard.app.config["MAX_CONTENT_LENGTH"]
    print(f"  MAX_CONTENT_LENGTH {dashboard.app.config['MAX_CONTENT_LENGTH']} "
          f">= Discord's {api.SOUND_MAX_BYTES} OK")

    print("\n=== Discord being unreachable explains itself ===")
    def down(g):
        raise api.DiscordError("Discord returned 500")
    api.guild_sounds = down
    body = c.get(f"/servers/{GUILD}/soundboard").data.decode()
    assert "Can't read this server's sounds" in body
    assert "Discord returned 500" in body
    print("  the page says so rather than showing an empty board OK")

    print("\nALL CHECKS PASSED")


main()
