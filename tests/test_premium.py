"""The premium page.

Nothing on it takes money yet, and that is the part worth pinning down. A pricing page that
shows a buy button before there is anywhere to buy from is worse than one that says so, so the
checks here are mostly about the page being honest when the checkout links are unset.

The saving on the yearly plan is worked out from the two prices rather than typed in, because
both come from the environment and a hand-written "2 months free" would go stale the first
time one of them moved.
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


class FakeColl:
    def __init__(self, name): self.name = name
    def find_one(self, *a, **k): return None
    def find(self, *a, **k): return []
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


def page(client) -> str:
    return html.unescape(client.get("/premium").data.decode())


def main():
    c = dashboard.app.test_client()

    print("=== it loads, and needs no login ===")
    r = c.get("/premium")
    assert r.status_code == 200, r.status_code
    # Somebody weighing up whether to pay should not meet a sign-in form first.
    assert "/login" not in r.headers.get("Location", "")
    print("  200 while logged out OK")

    print("\n=== both tiers are on the page ===")
    body = page(c)
    plans = dashboard.premium_plans()
    assert [p["id"] for p in plans] == ["monthly", "yearly"], plans
    for plan in plans:
        assert plan["price"] in body, plan
        assert plan["cadence"] in body, plan
        assert plan["name"] in body, plan
    print(f"  {plans[0]['price']} {plans[0]['cadence']} and "
          f"{plans[1]['price']} {plans[1]['cadence']} OK")

    print("\n=== the yearly saving is worked out, not typed ===")
    for monthly, yearly, expected in (("3", "30", "2 months free"),
                                      ("2.99", "29.99", "2 months free"),
                                      ("5", "55", "1 month free"),
                                      ("5", "45", "3 months free"),
                                      # Not close enough to a whole number of months, so it
                                      # gives the amount rather than rounding into a fib.
                                      ("5", "52", "Save $8"),
                                      ("5", "58", "Save $2"),
                                      ("5", "60", ""),      # no saving at all
                                      ("5", "70", ""),      # dearer, so nothing to claim
                                      ("5", "0", ""),       # free isn't a saving, it's a bug
                                      ("free", "30", "")):  # not a number
        got = dashboard.premium_saving(monthly, yearly)
        assert got == expected, (monthly, yearly, got, expected)
        print(f"  {monthly} a month vs {yearly} a year -> {got or 'nothing claimed'}")
    assert plans[1]["saving"], "the yearly plan carries the one it worked out"
    assert not plans[0]["saving"], "the monthly plan has nothing to claim"
    # The toggle needs scripting, so the saving has to be in the words on the card as well or
    # a reader without it never hears about it.
    assert plans[1]["saving"].lower() in plans[1]["blurb"], plans[1]["blurb"]
    print(f"  and it's in the yearly blurb too: {plans[1]['blurb']}")

    print("\n=== with no checkout set, it says so rather than selling ===")
    assert not dashboard.PREMIUM_CHECKOUT_MONTHLY and not dashboard.PREMIUM_CHECKOUT_YEARLY, \
        "the default is unset, and this suite relies on it"
    assert "Not open yet" in body
    assert "Premium isn't open yet" in body
    assert "aria-disabled" in body
    # The one thing that must not happen: a button that looks live and goes nowhere.
    assert 'href=""' not in body and "href='#'" not in body
    print("  the buy buttons are disabled and the page explains why OK")

    print("\n=== and starts selling once one is ===")
    dashboard.PREMIUM_CHECKOUT_MONTHLY = "https://pay.example.test/monthly"
    dashboard.PREMIUM_CHECKOUT_YEARLY = "https://pay.example.test/yearly"
    try:
        live = page(c)
        assert "https://pay.example.test/monthly" in live
        assert "https://pay.example.test/yearly" in live
        assert "Not open yet" not in live
        assert "Premium isn't open yet" not in live
        print("  both links appear and the notice goes OK")
    finally:
        dashboard.PREMIUM_CHECKOUT_MONTHLY = ""
        dashboard.PREMIUM_CHECKOUT_YEARLY = ""
    assert "Not open yet" in page(c), "and back to disabled when they're unset again"

    print("\n=== the extras are marked as placeholders ===")
    assert len(dashboard.PREMIUM_FEATURES) >= 3, dashboard.PREMIUM_FEATURES
    for icon, title, blurb in dashboard.PREMIUM_FEATURES:
        assert icon and title and blurb, (icon, title, blurb)
        assert title in body, title
    # Nobody should be able to read this list as a set of things they can buy today.
    assert "Placeholder" in body
    print(f"  {len(dashboard.PREMIUM_FEATURES)} slots, all saying they're placeholders OK")

    print("\n=== free is a column beside the paid ones, not a footnote ===")
    assert dashboard.PREMIUM_FREE, "the free column isn't a placeholder"
    for item in dashboard.PREMIUM_FREE:
        assert item in body, item
    assert "stays free" in body
    # Priced at zero in the same currency, so the columns read as one row to compare rather
    # than a pitch with a disclaimer under it.
    assert dashboard.PREMIUM_CURRENCY + "0" in body
    # And it carries the invite, because the answer to "what do I get for nothing" is a bot
    # you can add right now.
    assert dashboard.invite_url() in body, "the free column links the invite"
    # The paid columns have to say they carry the free one rather than replace it.
    assert "Everything in Free" in body
    print(f"  {len(dashboard.PREMIUM_FREE)} free features, priced at "
          f"{dashboard.PREMIUM_CURRENCY}0, with the invite on it OK")

    print("\n=== both plans are there without scripting ===")
    # The toggle only hides one of them, so a page with no script has to carry both. Neither
    # is behind a fetch, and the css that hides one is gated on the .js flag.
    for plan in plans:
        assert f'data-plan="{plan["id"]}"' in body, plan["id"]
    assert 'data-billing' in body
    print("  both cards render server side OK")

    print("\n=== it's linked from every page ===")
    for path in ("/", "/docs", "/status", "/support", "/premium"):
        assert '/premium"' in c.get(path).data.decode(), path
    print("  in the header and the footer, everywhere OK")

    print("\n=== and carries the terms and privacy footer like the rest ===")
    assert dashboard.TERMS_URL in body and dashboard.PRIVACY_URL in body
    print("  both present OK")

    print("\nALL CHECKS PASSED")


main()
