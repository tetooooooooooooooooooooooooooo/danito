"""The dashboard.

Runs as a separate process from the bot and shares only MongoDB. Everything security-relevant
is deliberately re-checked per request rather than trusted from the session:

- The OAuth callback verifies a `state` value it generated, so another site can't complete a
  login on somebody's behalf.
- Every guild page and every save re-asks Discord whether this user really can manage that
  guild. Being shown a server in the picker earlier is not permission to edit it later.
- Form posts carry a token tied to the session, so a link on another site can't make a logged
  in admin change their settings.
- Submitted channel ids are checked against that guild's real channels, so a crafted post
  can't aim the bot at a channel somewhere else.
"""

import json
import math
import os
import secrets
from functools import wraps

from dotenv import load_dotenv

# Must run before discord_api is imported: that module reads its credentials at import time.
# On Heroku the real environment already exists and load_dotenv leaves it alone, so this only
# matters when running locally from a .env file.
load_dotenv()

from flask import (Flask, Response, abort, flash, redirect, render_template,  # noqa: E402
                   request, session, url_for)
from werkzeug.exceptions import HTTPException                                # noqa: E402

import discord_api as api                                                    # noqa: E402
import changelog                                                             # noqa: E402
import docs                                                                  # noqa: E402
import store                                                                 # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)

# The bot's own name inside Discord comes from the application itself, so it follows a rename
# in the Developer Portal without a deploy. Only the web pages need telling.
BRAND = os.environ.get("DASHBOARD_BRAND", "Newt")

# The permissions the invite asks for, in one place rather than repeated in each template.
#
# Manage Server (0x20) was added for invite tracking. Discord tells a bot nothing about how
# somebody joined, so the only way to know is to read every invite's use count and watch which
# one moves, and reading them needs this. Without it the insights page still works, it just
# reports every join as coming from an unknown invite and says why.
#
# Anybody who added the bot before this went in keeps the old permissions until they add it
# again: Discord does not widen a grant retrospectively.
INVITE_PERMISSIONS = "1374389534326"

# The terms and privacy policy live outside this app, on their own static site, so they stay
# up whether or not the dashboard is. Discord wants both reachable from anywhere the bot is
# offered, which is why the footer carrying them is on every page rather than only when
# somebody is signed in.
# `or` rather than a get() default: .env.example lists these empty, and an empty string would
# otherwise win over the default and produce a link that goes nowhere.
TERMS_URL = (os.environ.get("TERMS_URL") or
             "https://tetooooooooooooooooooooooooooo.github.io/soundcord-tos/").strip()
PRIVACY_URL = (os.environ.get("PRIVACY_URL") or
               "https://tetooooooooooooooooooooooooooo.github.io/soundcord-tos/privacy.html"
               ).strip()


# Discord will send somebody back here after they add the bot, but only to an address
# registered in the Developer Portal. Left unset the invite behaves exactly as before, because
# an unregistered address makes Discord refuse the whole invite rather than skip the redirect.
INVITE_REDIRECT_URI = (os.environ.get("INVITE_REDIRECT_URI") or "").strip()


def invite_url(guild_id=None) -> str:
    url = (f"https://discord.com/oauth2/authorize?client_id={api.CLIENT_ID}"
           f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")
    if guild_id:
        url += f"&guild_id={guild_id}"
    if INVITE_REDIRECT_URI:
        from urllib.parse import quote
        url += f"&response_type=code&redirect_uri={quote(INVITE_REDIRECT_URI, safe='')}"
    return url
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Heroku terminates TLS in front of the dyno, so cookies can be secure-only in production.
    SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_INSECURE_COOKIES") != "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)

if not os.environ.get("DASHBOARD_SECRET_KEY"):
    print("[dashboard] DASHBOARD_SECRET_KEY is unset, so a random one is in use. "
          "Everyone gets logged out on every restart. Set it.")


# ── session helpers ──────────────────────────────────────────────────
def current_user():
    return session.get("user")


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf():
    sent = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    # compare_digest so a wrong token can't be guessed a character at a time.
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, "This form expired. Go back, reload the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not current_user() or not session.get("token"):
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


def needs_discord(view):
    """Refuse to write anything while Discord isn't answering.

    Every save on this dashboard validates what was submitted against the guild's real
    channels and roles, which means asking Discord. When that call fails there is no valid
    set to check against, and carrying on would treat "I couldn't ask" as "none of these
    exist" and save an empty result over whatever was there. Better to change nothing and
    say so.
    """
    @wraps(view)
    def wrapper(*a, **kw):
        try:
            return view(*a, **kw)
        except api.DiscordError:
            flash("Discord isn't answering just now, so nothing was changed. This is nothing "
                  "to do with your settings. Try again in a moment.")
            guild_id = kw.get("guild_id")
            return redirect(url_for("guild_settings", guild_id=guild_id) if guild_id
                            else url_for("servers"))
    return wrapper


def require_guild(guild_id: int) -> dict:
    """Confirm, right now, that the logged in user can manage this guild and the bot is in it.

    Re-checked on every request on purpose. A session that listed a guild an hour ago says
    nothing about whether that person still administers it.
    """
    user = current_user()
    token = session.get("token")
    if not user or not token:
        abort(401)
    try:
        allowed = api.manageable_guilds(token, int(user["id"]))
    except api.DiscordError:
        session.clear()
        abort(401)

    match = next((g for g in allowed if int(g["id"]) == guild_id), None)
    if match is None:
        # Same response whether the guild doesn't exist or they simply can't touch it, so the
        # dashboard isn't a way to probe which servers exist.
        abort(404)
    if guild_id not in store.bot_guild_ids():
        abort(404)
    return match


def absolute(path: str = "") -> str:
    """A full url for the current host, for the link preview tags.

    Open Graph will not follow a relative path, so these have to be absolute. The scheme is
    forced to https off localhost because Heroku terminates TLS in front of the dyno and hands
    the app a plain http request, which would otherwise put http:// in every shared link.
    """
    root = (os.environ.get("SITE_URL") or request.url_root).rstrip("/")
    if root.startswith("http://") and not root.startswith(("http://127.0.0.1",
                                                           "http://localhost")):
        root = "https://" + root[len("http://"):]
    return root + path


@app.context_processor
def inject():
    return {"user": current_user(), "csrf_token": csrf_token, "api": api,
            "brand": BRAND, "invite_url": invite_url,
            "terms_url": TERMS_URL, "privacy_url": PRIVACY_URL,
            "page_url": absolute(request.path),
            "og_image": absolute(url_for("static", filename="og.png"))}


# ── auth ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    problems = api.configured()
    if problems:
        return render_template("misconfigured.html", problems=problems), 503
    # Shown to everybody, signed in or not. It used to bounce anybody logged in straight to
    # their server list, which meant the brand in the header was a link back to the dashboard
    # you were already in, and there was no way to reach the front page at all without
    # logging out. The dashboard has its own button in the header instead.
    #
    # The prices are on the landing page as well as on /premium. Somebody who reads the whole
    # pitch and never learns there is a paid tier is a worse outcome than one who sees the
    # number early and decides it is fine.
    return render_template("landing.html", plans=premium_plans(), premium_on_sale=on_sale())


@app.route("/docs")
def documentation():
    """Public on purpose: somebody deciding whether to add the bot should be able to read what
    it does without handing over an account first."""
    return render_template("docs.html", setup=docs.SETUP, sections=docs.SECTIONS,
                           troubleshooting=docs.TROUBLESHOOTING)


# ── status ───────────────────────────────────────────────────────────
# Public, and deliberately the one page that needs no login and no database write. Somebody
# whose bot has gone quiet wants an answer, not a sign-in form.
STATUS_WORDS = {
    "up": ("All good", "Newt is online and answering."),
    "wobbly": ("Having a moment", "Newt hasn't checked in for a few minutes. This is usually a "
                                  "restart or a deploy and clears itself."),
    "down": ("Offline", "Newt hasn't checked in for a while, so commands won't be working."),
    "unknown": ("Not sure", "There's nothing recent to go on."),
}


def _ago(seconds) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    days = hours // 24
    return f"{days} day{'' if days == 1 else 's'}"


def status_payload() -> dict:
    status = store.bot_status()
    heading, detail = STATUS_WORDS[status["state"]]
    return {
        **status,
        "heading": heading,
        "detail": status.get("reason") or detail,
        "quiet_for": _ago(status.get("seconds_quiet")),
        "uptime": _ago(status.get("uptime_seconds")),
        # The page you are reading is served by the web process, so its being up is a given.
        "dashboard": "up",
    }


@app.route("/status")
def status():
    return render_template("status.html", status=status_payload(),
                           brand=BRAND)


@app.route("/status.json")
def status_json():
    """Polled by the page so it updates without a reload, and usable by anything else."""
    data = status_payload()
    for key in ("last_seen", "started_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


# ── support ──────────────────────────────────────────────────────────
# The invite is a placeholder until there is a server to point at. When it hasn't been set the
# page says so rather than offering a link that goes nowhere, and leans on tickets instead.
PLACEHOLDER_INVITE = "https://discord.gg/placeholder"
SUPPORT_INVITE = (os.environ.get("SUPPORT_INVITE") or PLACEHOLDER_INVITE).strip()


@app.route("/support")
def support():
    user = current_user()
    mine, guilds = [], []
    if user:
        mine = store.tickets_for(int(user["id"]))
        try:
            # Only servers they administer, so a ticket can't be filed against somebody else's.
            guilds = sorted(api.manageable_guilds(session["token"], int(user["id"])),
                            key=lambda g: g["name"].lower())
        except api.DiscordError:
            guilds = []          # the picker is optional, so a Discord blip isn't fatal
    return render_template(
        "support.html",
        tickets=mine,
        user_guilds=guilds,
        categories=store.TICKET_CATEGORIES,
        category_labels=store.TICKET_CATEGORY_LABELS,
        support_invite=SUPPORT_INVITE,
        invite_ready=SUPPORT_INVITE != PLACEHOLDER_INVITE,
        max_subject=store.MAX_SUBJECT,
        max_body=store.MAX_BODY,
    )


@app.route("/support/new", methods=["POST"])
@login_required
def new_ticket():
    check_csrf()
    user = current_user()
    user_id = int(user["id"])

    blocked = store.can_open_ticket(user_id)
    if blocked:
        flash(blocked)
        return redirect(url_for("support"))

    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not subject or not body:
        flash("A ticket needs a subject and a description.")
        return redirect(url_for("support"))

    # A server can only be attached if they really administer it, so a ticket can't be filed
    # against somebody else's server.
    guild_id, guild_name = None, None
    raw = request.form.get("guild_id") or ""
    if raw:
        try:
            allowed = api.manageable_guilds(session["token"], user_id)
        except api.DiscordError:
            allowed = []
        match = next((g for g in allowed if g["id"] == raw), None)
        if match:
            guild_id, guild_name = int(match["id"]), match["name"]

    number = store.open_ticket(
        user_id, user["username"], request.form.get("category", "other"),
        subject, body, guild_id, guild_name)
    flash(f"Ticket #{number} is open. You'll get a direct message when somebody replies.")
    return redirect(url_for("support") + f"#ticket-{number}")


@app.route("/support/<int:number>/reply", methods=["POST"])
@login_required
def reply_ticket(number: int):
    check_csrf()
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Nothing to send.")
    elif store.reply_to_ticket(int(current_user()["id"]), number, body):
        flash(f"Added to ticket #{number}.")
    else:
        flash("That ticket is closed, or isn't yours.")
    return redirect(url_for("support") + f"#ticket-{number}")


@app.route("/support/<int:number>/close", methods=["POST"])
@login_required
def close_ticket_route(number: int):
    check_csrf()
    if store.close_ticket(int(current_user()["id"]), number):
        flash(f"Ticket #{number} is closed. Thanks.")
    else:
        flash("That ticket is already closed, or isn't yours.")
    return redirect(url_for("support"))


# ── premium ──────────────────────────────────────────────────────────
# Nothing here takes money yet. The prices and the plans are real enough to show, but the
# checkout links come from the environment and are unset, so every buy button says the page
# isn't open rather than leading somewhere that would charge somebody. Same shape as
# SUPPORT_INVITE above: a missing piece is visible on the page instead of hidden.
PREMIUM_CURRENCY = (os.environ.get("PREMIUM_CURRENCY") or "$").strip()
PREMIUM_PRICE_MONTHLY = (os.environ.get("PREMIUM_PRICE_MONTHLY") or "2.99").strip()
PREMIUM_PRICE_YEARLY = (os.environ.get("PREMIUM_PRICE_YEARLY") or "29.99").strip()
PREMIUM_CHECKOUT_MONTHLY = (os.environ.get("PREMIUM_CHECKOUT_MONTHLY") or "").strip()
PREMIUM_CHECKOUT_YEARLY = (os.environ.get("PREMIUM_CHECKOUT_YEARLY") or "").strip()

# Placeholders, every one of them. These are slots waiting for real features, not a list of
# things anybody can buy today, and the page says so out loud. Replace the title and the line
# under it as each one becomes real. Keep them in the order you want them read.
PREMIUM_FEATURES = [
    ("📊", "Premium feature one", "Placeholder. Say what this adds and who it's for."),
    ("🗂️", "Premium feature two", "Placeholder. Say what this adds and who it's for."),
    ("⚡", "Premium feature three", "Placeholder. Say what this adds and who it's for."),
    ("🎨", "Premium feature four", "Placeholder. Say what this adds and who it's for."),
    ("🔎", "Premium feature five", "Placeholder. Say what this adds and who it's for."),
    ("🤝", "Premium feature six", "Placeholder. Say what this adds and who it's for."),
]

# The free side is not a placeholder: these all work today, and the point of listing them is
# that none of them move behind the paywall later.
PREMIUM_FREE = [
    "Retention figures and the joining groups behind them",
    "The 1 to 10 survey, and every rating it collects",
    "Moderation with numbered cases, and automod's nine rules",
    "Role buttons, autorole, welcome and goodbye messages",
    "Logging, deleted media, and the Discovery readiness check",
]


def premium_saving(monthly: str, yearly: str) -> str:
    """How much a year up front saves, worked out rather than typed in.

    Both prices can be changed from the environment, so a saving written by hand would go
    stale the first time one of them moved. Returns an empty string when the sums don't work
    out to a saving, which is also what a price that isn't a number gets.
    """
    try:
        twelve, year = float(monthly) * 12, float(yearly)
    except (TypeError, ValueError):
        return ""
    if year <= 0 or year >= twelve:
        return ""
    saved = twelve - year
    months = saved / float(monthly)
    # Whole months read better than a percentage, but only when it really is about a whole
    # number of them. Otherwise fall back to the amount.
    if abs(months - round(months)) < 0.15 and round(months) >= 1:
        whole = round(months)
        return f"{whole} month{'' if whole == 1 else 's'} free"
    return f"Save {PREMIUM_CURRENCY}{saved:.2f}".replace(".00", "")


def premium_plans() -> list:
    saving = premium_saving(PREMIUM_PRICE_MONTHLY, PREMIUM_PRICE_YEARLY)
    return [
        {
            "id": "monthly",
            "name": "Monthly",
            "price": PREMIUM_CURRENCY + PREMIUM_PRICE_MONTHLY,
            "cadence": "a month",
            "blurb": "Month to month. Cancel it whenever you like.",
            "saving": "",
            "checkout": PREMIUM_CHECKOUT_MONTHLY,
        },
        {
            "id": "yearly",
            "name": "Yearly",
            "price": PREMIUM_CURRENCY + PREMIUM_PRICE_YEARLY,
            "cadence": "a year",
            # The saving goes in the words as well as on the toggle, because the toggle needs
            # scripting and this line doesn't.
            "blurb": (f"One payment a year, which works out at {saving.lower()}." if saving
                      else "One payment a year rather than twelve."),
            "saving": saving,
            "checkout": PREMIUM_CHECKOUT_YEARLY,
        },
    ]


def on_sale() -> bool:
    """Whether there is anywhere to send somebody who wants to pay.

    With no checkout link the pages show the prices but drop the buy buttons, rather than
    offering one that leads nowhere.
    """
    return bool(PREMIUM_CHECKOUT_MONTHLY or PREMIUM_CHECKOUT_YEARLY)


@app.route("/premium")
def premium():
    """Public, like the docs: somebody deciding whether this is worth paying for should be
    able to read the prices without signing in first."""
    return render_template("premium.html", plans=premium_plans(),
                           features=PREMIUM_FEATURES, free=PREMIUM_FREE,
                           # The free column prices itself at zero, in the same currency as
                           # the paid ones, so the three read as one row rather than three.
                           currency=PREMIUM_CURRENCY,
                           on_sale=on_sale())


@app.route("/added")
def added():
    """Where Discord sends somebody after they add the bot.

    Reachable directly as well, since the redirect only happens once the address is registered
    and somebody may well arrive here from a link.
    """
    guild_id = request.args.get("guild_id") or ""
    guild_id = guild_id if guild_id.isdigit() else ""
    return render_template("added.html", guild_id=guild_id)


# ── crawlers ─────────────────────────────────────────────────────────
# The pages worth indexing, in the order they matter. Everything else is either behind a
# login, a redirect somewhere else, or a form post, and none of that belongs in a search
# result. Endpoint names rather than paths so a renamed route can't leave a dead entry here.
PUBLIC_PAGES = ["index", "documentation", "premium", "support", "status", "whats_new"]

# Crawling these achieves nothing and costs a Discord round trip each time. /callback and
# /added take query parameters that mean nothing without the request that produced them.
PRIVATE_PATHS = ["/servers", "/login", "/callback", "/logout", "/added"]


@app.route("/robots.txt")
def robots():
    lines = ["User-agent: *"]
    lines += [f"Disallow: {path}" for path in PRIVATE_PATHS]
    lines += ["Allow: /", "", f"Sitemap: {absolute(url_for('sitemap'))}", ""]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    urls = "".join(f"<url><loc>{absolute(url_for(name))}</loc></url>"
                   for name in PUBLIC_PAGES)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'{urls}</urlset>')
    return Response(xml, mimetype="application/xml")


@app.route("/changelog")
def whats_new():
    return render_template("changelog.html", entries=changelog.ENTRIES,
                           kinds=changelog.KINDS)


@app.route("/login")
def login():
    if api.configured():
        return redirect(url_for("index"))
    state = secrets.token_urlsafe(24)
    session["state"] = state
    nxt = request.args.get("next", "")
    # Only ever store a path of our own, never an absolute url somebody supplied.
    session["next"] = nxt if nxt.startswith("/") and not nxt.startswith("//") else ""
    return redirect(api.authorize_url(state))


@app.route("/callback")
def callback():
    expected = session.pop("state", None)
    got = request.args.get("state")
    if not expected or not got or not secrets.compare_digest(expected, got):
        return render_template("error.html",
                               message="That login didn't come from here. Try again."), 400

    code = request.args.get("code")
    if not code:
        return redirect(url_for("index"))

    try:
        payload = api.exchange_code(code)
        token = payload["access_token"]
        user = api.get_user(token)
    except (api.DiscordError, KeyError):
        return render_template("error.html",
                               message="Discord wouldn't complete that login. Try again."), 502

    session["token"] = token
    session["user"] = {"id": user["id"], "username": user.get("global_name")
                       or user.get("username"), "avatar": api.avatar_url(user)}
    api.forget_user(int(user["id"]))          # start from a fresh guild list on each login
    destination = session.pop("next", "") or url_for("servers")
    return redirect(destination)


@app.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    user = current_user()
    if user:
        api.forget_user(int(user["id"]))
    session.clear()
    return redirect(url_for("index"))


# ── servers ──────────────────────────────────────────────────────────
# Which tab each form's section lives in. Related settings share a tab, so a save has to know
# where to send you back to rather than assuming its own name is a pane.
SECTION_TABS = {
    "welcome": "greetings", "goodbye": "greetings",
    "autorole": "roles",
    "modlog": "logging", "medialog": "logging", "pinglog": "logging",
}


@app.route("/servers")
@login_required
def servers():
    user = current_user()
    try:
        allowed = api.manageable_guilds(session["token"], int(user["id"]))
    except api.DiscordError:
        session.clear()
        return redirect(url_for("index"))

    present = store.bot_guild_ids()
    joined, absent = [], []
    for g in sorted(allowed, key=lambda x: x["name"].lower()):
        (joined if int(g["id"]) in present else absent).append(g)
    return render_template("servers.html", joined=joined, absent=absent)


@app.route("/servers/<int:guild_id>")
@login_required
def guild_settings(guild_id: int):
    guild = require_guild(guild_id)
    # The page is still worth rendering when Discord won't answer: the tabs, the switches and
    # everything already saved are all readable. Only the dropdowns need it. What matters is
    # that the page says which of the two happened, rather than showing empty dropdowns under
    # a warning about a permission that is perfectly fine.
    try:
        roles = api.guild_roles(guild_id)
        channels = api.guild_channels(guild_id)
        discord_ok = True
    except api.DiscordError:
        roles, channels, discord_ok = [], [], False

    panels = store.panels(guild_id)
    for panel in panels:
        # Keyed by role id so the template can fill each row's label and emoji boxes without
        # searching the list again for every role in the server.
        panel["by_role"] = {int(e["role_id"]): e for e in (panel.get("roles") or [])}

    return render_template(
        "settings.html",
        guild=guild,
        settings=store.settings(guild_id),
        channels=channels,
        roles=roles,
        discord_ok=discord_ok,
        panels=panels,
        role_names={r["id"]: r for r in roles},
        channel_names={c["id"]: c for c in channels},
        max_autoroles=store.MAX_AUTOROLES,
        max_panel_roles=store.MAX_PANEL_ROLES,
        log_events=store.LOG_EVENTS,
        automod_rules=store.AUTOMOD_RULES,
        automod_actions=store.AUTOMOD_ACTIONS,
        automod_defaults=store.AUTOMOD_DEFAULTS,
        minage_range=store.MINAGE_RANGE,
        minage_default=store.MINAGE_DEFAULT,
        minage_actions=store.MINAGE_ACTIONS,
    )


# ── the trend chart ──────────────────────────────────────────────────
# Drawn as inline SVG worked out here rather than by a charting library. Nothing loads from a
# CDN anywhere on this site, and a line with a few gaps in it does not justify 90KB of
# JavaScript. Doing the arithmetic in Python also keeps the template readable, which the same
# sums written in Jinja would not be.
CHART = {"w": 720, "h": 210, "left": 38, "right": 12, "top": 12, "bottom": 30}

# What one point covers, for the line under the heading.
UNIT_WORDS = {"daily": "day", "weekly": "week", "monthly": "month"}


def count_axis(peak: int) -> tuple:
    """A top value and tick marks for a chart counting people.

    Percentages could hardcode 0/25/50/75/100. Counts can't: a server with 7 joins on its best
    day and one with 4,000 need different scales, and both need labels that are whole people
    rather than 2.5 of one. Steps run 1, 2, 5, 10, 20, 50 and up, and the smallest one that
    covers the peak in five intervals or fewer wins, so there are never more than six labels.

    Floored at 4 so a quiet server gets a sensible axis instead of one that tops out at 1 and
    pins its own line to the ceiling.
    """
    peak = max(int(peak or 0), 4)
    step, magnitude = 1, 1
    while peak / step > 5:
        # 1, 2, 5, 10, 20, 50, 100 ... rather than doubling, which skips every 5.
        base = step // magnitude
        if base == 1:
            step = 2 * magnitude
        elif base == 2:
            step = 5 * magnitude
        else:
            magnitude *= 10
            step = magnitude
    top = step * math.ceil(peak / step)
    return top, [step * i for i in range(top // step + 1)]


def activity_chart(activity: dict, series: str = store.DEFAULT_SERIES) -> dict:
    """Joins and leaves over time, as one or two lines against a shared axis.

    Zero is a real answer for both, so unlike the retention chart these never break: a day
    nobody joined is a point on the floor, not a gap.
    """
    points = activity["points"]
    plot_w = CHART["w"] - CHART["left"] - CHART["right"]
    plot_h = CHART["h"] - CHART["top"] - CHART["bottom"]
    span = max(len(points) - 1, 1)
    top, ticks = count_axis(activity["peak"])

    def x_of(i):
        return CHART["left"] + (plot_w / 2 if len(points) == 1 else i * plot_w / span)

    def y_of(value):
        return CHART["top"] + (1 - value / top) * plot_h

    # Both series are always worked out, whichever is being shown. The browser switches
    # between them without asking the server again, so it needs all of it up front.
    lines = {}
    for name in ("joins", "leaves"):
        lines[name] = {
            "name": name,
            "label": store.SERIES[name],
            "total": activity[name],
            "points": [{"x": round(x_of(i), 1), "y": round(y_of(p[name]), 1),
                        "value": p[name], "label": p["label"]}
                       for i, p in enumerate(points)],
        }

    every = max(1, len(points) // 8)
    labels = [{"x": round(x_of(i), 1), "text": p["label"]}
              for i, p in enumerate(points)
              if i % every == 0 or i == len(points) - 1]

    # One invisible column per bucket, so hovering anywhere above a point works. Aiming at a
    # 3.5px dot with a finger is not a thing anybody should have to do.
    half = (plot_w / span / 2) if len(points) > 1 else plot_w / 2
    hits = []
    for i, point in enumerate(points):
        centre = x_of(i)
        left = max(CHART["left"], centre - half)
        right = min(CHART["w"] - CHART["right"], centre + half)
        hits.append({"x": round(left, 1), "w": round(right - left, 1),
                     "centre": round(centre, 1), "label": point["label"],
                     "joins": point["joins"], "leaves": point["leaves"]})

    return {
        **CHART,
        "series": series,
        "period": activity["period"],
        "heading": activity["heading"],
        "unit": UNIT_WORDS[activity["period"]],
        "lines": lines,
        # What the template loops over: only the series on show, in draw order.
        "shown": [lines[n] for n in (("joins", "leaves") if series == "both" else (series,))],
        "labels": labels,
        "hits": hits,
        "gridlines": [{"y": round(y_of(t), 1), "value": t} for t in ticks],
        "baseline": round(y_of(0), 1),
        "top": CHART["top"],
        "totals": {"joins": activity["joins"], "leaves": activity["leaves"],
                   "net": activity["joins"] - activity["leaves"]},
        # Nobody has joined or left in the whole period, so the lines would all sit flat on
        # the floor and say nothing. The page draws the axis and explains instead.
        "empty": activity["joins"] == 0 and activity["leaves"] == 0,
    }


@app.route("/servers/<int:guild_id>/insights")
@login_required
def guild_insights(guild_id: int):
    """The numbers, drawn rather than listed.

    Separate from the settings page on purpose. Settings is a form you come to with something
    to change; this is a page you come to with a question, and putting a chart behind a
    settings tab hides it from everybody who is not already editing something.
    """
    guild = require_guild(guild_id)
    period = request.args.get("period", store.DEFAULT_TREND)
    # Both toggles are links with their own url, so a chart can be sent to somebody and the
    # back button does what it should. Anything unrecognised falls back rather than erroring.
    series = request.args.get("series", store.DEFAULT_SERIES)
    if series not in store.SERIES:
        series = store.DEFAULT_SERIES
    data = store.insights(guild_id, period)
    # Every period's geometry, so the toggles can redraw in the browser rather than asking for
    # the page again. The one asked for is also rendered server side, so the chart is there
    # before any script runs and the links still work without one.
    charts = {name: activity_chart(activity, series)
              for name, activity in data["activity"].items()}
    return render_template("insights.html", guild=guild, data=data,
                           chart=charts[period if period in charts else store.DEFAULT_TREND],
                           charts=charts, chart_json=json.dumps(charts),
                           series=series, all_series=store.SERIES,
                           periods=store.TREND_PERIODS)


@app.route("/servers/<int:guild_id>/embed")
@login_required
def embed_builder(guild_id: int):
    """Build a message and post it as the bot.

    Its own page rather than a settings tab: nothing here is a setting. You come to it with
    something to say, send it, and leave.
    """
    guild = require_guild(guild_id)
    try:
        channels = api.guild_channels(guild_id)
        discord_ok = True
    except api.DiscordError:
        channels, discord_ok = [], False
    return render_template("embed.html", guild=guild, channels=channels,
                           discord_ok=discord_ok, limits=store.EMBED_MAX,
                           max_fields=store.MAX_EMBED_FIELDS)


@app.route("/servers/<int:guild_id>/embed", methods=["POST"])
@login_required
@needs_discord
def send_embed(guild_id: int):
    check_csrf()
    require_guild(guild_id)

    # The same check every other save makes: a channel id typed into a request is not a
    # channel in this server until Discord says it is.
    valid = {int(c["id"]): c for c in api.guild_channels(guild_id)}
    try:
        channel_id = int(request.form.get("channel_id") or 0)
    except ValueError:
        channel_id = 0
    if channel_id not in valid:
        flash("Pick a channel in this server.")
        return redirect(url_for("embed_builder", guild_id=guild_id))

    payload, problems = store.clean_embed(request.form)
    if problems:
        # All of them at once. Fixing one thing to be told about the next is the worst way to
        # fill in a form this long.
        for problem in problems[:6]:
            flash(problem)
        return redirect(url_for("embed_builder", guild_id=guild_id))

    try:
        api.post_message(channel_id, payload)
    except api.DiscordError as e:
        flash(f"Discord wouldn't send it. {e}")
        return redirect(url_for("embed_builder", guild_id=guild_id))

    flash(f"Sent to #{valid[channel_id]['name']}.")
    # sent=1 is how the page knows to throw the saved draft away. It cannot be done when the
    # form is submitted, because every route back here is the same redirect and a refused
    # message would take somebody's work with it.
    return redirect(url_for("embed_builder", guild_id=guild_id, sent=1))


@app.route("/servers/<int:guild_id>", methods=["POST"])
@login_required
@needs_discord
def save_guild_settings(guild_id: int):
    check_csrf()
    require_guild(guild_id)

    valid_channels = {int(c["id"]) for c in api.guild_channels(guild_id)}
    valid_roles = assignable_role_ids(guild_id)
    section = request.form.get("section", "")

    # Automod, like logging, writes one nested document rather than a set of flat fields, so
    # it has its own builder instead of going through the generic allow-list.
    if section == "automod":
        # Exemptions can name any role, not only ones the bot could hand out, since being
        # exempt from a filter has nothing to do with whether the bot can assign it.
        every_role = {int(r["id"]) for r in api.guild_roles(guild_id)}
        existing = (store.settings(guild_id) or {}).get("automod") or {}
        store.save(guild_id, {"automod": store.clean_automod(
            request.form, valid_channels, every_role, existing)})
        flash("Saved. The bot picks this up within a few seconds.")
        return redirect(url_for("guild_settings", guild_id=guild_id) + "#automod")

    # Logging is handled apart from the table below: it writes one nested map of twelve events
    # rather than a set of flat fields, so it can't go through the generic allow-list.
    if section == "logging":
        events = store.clean_log_events(request.form, valid_channels)
        values = {
            "logging_enabled": "logging_enabled" in request.form,
            "log_channel": store.clean("log_channel", request.form.get("log_channel"),
                                       valid_channels),
            "log_events": events,
        }
        # Every event needs somewhere to go: its own channel, or the shared one. With neither,
        # nothing would be recorded and the page would still claim logging was on.
        if not values["log_channel"] and not any(e["channel"] for e in events.values() if e["on"]):
            values["logging_enabled"] = False
        store.save(guild_id, values)
        flash("Saved. The bot picks this up within a few seconds.")
        return redirect(url_for("guild_settings", guild_id=guild_id) + "#logging")

    # Only the fields belonging to the submitted section are touched, so saving one card
    # can't blank the others.
    sections = {
        "medialog": ["medialog_enabled", "medialog_channel"],
        "modlog": ["modlog_channel"],
        "pinglog": ["pinglog_enabled", "pinglog_channel"],
        "welcome": ["welcome_enabled", "welcome_channel", "welcome_message", "welcome_embed"],
        "goodbye": ["goodbye_enabled", "goodbye_channel", "goodbye_message", "goodbye_embed"],
        "autorole": ["autorole_enabled", "autorole_ids"],
    }
    fields = sections.get(section)
    if not fields:
        abort(400, "Unknown section.")

    values = {}
    for field in fields:
        kind = store.ALLOWED_FIELDS[field]
        if kind is bool:
            raw = field in request.form          # a checkbox is absent when unticked
        elif kind == "role_ids":
            raw = request.form.getlist(field)    # a set of ticked boxes, not one value
        else:
            raw = request.form.get(field)
        values[field] = store.clean(field, raw, valid_channels, valid_roles)

    # A feature that needs somewhere to post can't be on without one.
    if section in ("medialog", "pinglog") and not values.get(f"{section}_channel"):
        values[f"{section}_enabled"] = False
    if section == "goodbye" and not values.get("goodbye_channel"):
        values["goodbye_enabled"] = False
    if section in ("welcome", "goodbye") and not values.get(f"{section}_message"):
        values[f"{section}_enabled"] = False
    # Nothing to hand out means nothing to switch on.
    if section == "autorole" and not values.get("autorole_ids"):
        values["autorole_enabled"] = False

    store.save(guild_id, values)
    flash("Saved. The bot picks this up within a few seconds.")
    # Several sections share a tab, so they have no pane of their own to come back to.
    # Without this a save would land on whatever tab happens to be first.
    anchor = SECTION_TABS.get(section, section)
    return redirect(url_for("guild_settings", guild_id=guild_id) + f"#{anchor}")


# ── role panels ──────────────────────────────────────────────────────
def assignable_role_ids(guild_id: int) -> set:
    """Only roles the bot could really give out. Enforced on save so a panel can't be built
    from roles that would fail silently every time somebody clicked them."""
    return {int(r["id"]) for r in api.guild_roles(guild_id) if not r["problem"]}


def _panel_form(guild_id: int):
    """The parts of a panel form that create and edit have in common."""
    valid_channels = {int(c["id"]) for c in api.guild_channels(guild_id)}
    channel_id = request.form.get("channel_id")
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        abort(400, "Pick a channel for this panel.")
    if channel_id not in valid_channels:
        abort(400, "That channel isn't in this server.")

    mode = request.form.get("mode", "toggle")
    title = request.form.get("title", "")
    description = request.form.get("description", "")
    return channel_id, title, description, mode


@app.route("/servers/<int:guild_id>/panels", methods=["POST"])
@login_required
@needs_discord
def create_panel(guild_id: int):
    check_csrf()
    require_guild(guild_id)
    channel_id, title, description, mode = _panel_form(guild_id)

    if not store.create_panel(guild_id, channel_id, title, description, mode):
        flash(f"That's the limit of {store.MAX_PANELS} panels. Delete one first.")
    else:
        flash("Panel created. Add some roles to it and it gets posted.")
    return redirect(url_for("guild_settings", guild_id=guild_id) + "#roles")


@app.route("/servers/<int:guild_id>/panels/<panel_id>", methods=["POST"])
@login_required
@needs_discord
def save_panel(guild_id: int, panel_id: str):
    check_csrf()
    require_guild(guild_id)

    if request.form.get("delete"):
        if store.delete_panel(guild_id, panel_id):
            flash("Panel deleted. The message disappears within a few seconds.")
        else:
            flash("That panel is already gone.")
        return redirect(url_for("guild_settings", guild_id=guild_id) + "#roles")

    channel_id, title, description, mode = _panel_form(guild_id)
    # Names as well as ids: a button left without a label should read as the role it grants,
    # not as a placeholder.
    usable = {int(r["id"]): r["name"] for r in api.guild_roles(guild_id) if not r["problem"]}

    # Each button is one ticked role plus its optional label and emoji, keyed by role id so a
    # missing tick drops the whole button rather than leaving an orphaned label behind.
    roles, seen = [], set()
    for raw in request.form.getlist("role_ids"):
        try:
            role_id = int(raw)
        except (TypeError, ValueError):
            continue
        if role_id not in usable or role_id in seen:
            continue
        seen.add(role_id)
        label = (request.form.get(f"label_{role_id}", "") or "").strip()
        roles.append({
            "role_id": role_id,
            "label": (label or usable[role_id])[:store.MAX_LABEL],
            "emoji": (request.form.get(f"emoji_{role_id}", "") or "").strip()[:64] or None,
        })

    if not store.save_panel(guild_id, panel_id, channel_id, title, description, mode, roles):
        abort(404)
    flash("Saved. The panel updates itself within a few seconds."
          if roles else "Saved. Tick at least one role and it gets posted.")
    return redirect(url_for("guild_settings", guild_id=guild_id) + "#roles")


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(404)
def handle(err):
    messages = {
        400: getattr(err, "description", "That request didn't look right."),
        401: "You need to log in again.",
        404: "That server isn't available to you.",
    }
    return render_template("error.html", message=messages.get(err.code, "Something went wrong.")), err.code


@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_crash(err):
    """Anything that got all the way out without being caught.

    Registered on Exception as well as 500 so a bug lands here rather than on Flask's own
    debug page, which leaks the traceback and looks nothing like the rest of the site. HTTP
    errors that already have their own handler are passed back untouched, or this would
    swallow every 404 as well.

    The exception is re-raised into the log first, since a page that says sorry and tells
    nobody is how a fault stays unfixed.
    """
    if isinstance(err, HTTPException) and err.code != 500:
        return err
    app.logger.exception("unhandled error at %s", request.path, exc_info=err)
    return render_template(
        "error.html",
        message="Something broke at our end, not yours. It's been logged. Try again in a "
                "moment, and if it keeps happening a ticket is the quickest way to get it "
                "looked at."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
