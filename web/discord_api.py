"""Talking to Discord: the OAuth dance for logging people in, and the few REST calls the
dashboard needs.

Two different credentials are in play and they must not be confused. The *user's* OAuth token
says who is logged in and which servers they belong to. The *bot's* token is what lists a
guild's channels. Never use the bot token to decide what a user is allowed to see.
"""

import os
import time

import requests

API = "https://discord.com/api/v10"
AUTHORIZE = "https://discord.com/oauth2/authorize"
TOKEN = f"{API}/oauth2/token"
SCOPES = "identify guilds"
TIMEOUT = 10

MANAGE_GUILD = 0x20          # the permission bit that makes somebody a server admin here

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Discord's guild list is rate limited, and a user reloading the picker shouldn't cost a call
# each time. Short enough that leaving a server is reflected quickly.
_GUILD_CACHE: dict[int, tuple[list, float]] = {}
GUILD_CACHE_TTL = 60


class DiscordError(RuntimeError):
    pass


def configured() -> list:
    """Which required settings are missing, so the app can say so instead of failing oddly."""
    missing = []
    for name, value in (("DISCORD_CLIENT_ID", CLIENT_ID),
                        ("DISCORD_CLIENT_SECRET", CLIENT_SECRET),
                        ("DISCORD_REDIRECT_URI", REDIRECT_URI),
                        ("BOT_TOKEN", BOT_TOKEN)):
        if not value:
            missing.append(name)
    return missing


def authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    return f"{AUTHORIZE}?" + urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "prompt": "none",
    })


def exchange_code(code: str) -> dict:
    resp = requests.post(
        TOKEN,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise DiscordError(f"token exchange failed ({resp.status_code})")
    return resp.json()


def _as_user(token: str, path: str):
    resp = requests.get(f"{API}{path}",
                        headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    if resp.status_code == 401:
        raise DiscordError("your login expired")
    if resp.status_code != 200:
        raise DiscordError(f"Discord returned {resp.status_code}")
    return resp.json()


def _as_bot(path: str):
    resp = requests.get(f"{API}{path}",
                        headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise DiscordError(f"Discord returned {resp.status_code}")
    return resp.json()


def get_user(token: str) -> dict:
    return _as_user(token, "/users/@me")


def manageable_guilds(token: str, user_id: int, force: bool = False) -> list:
    """Guilds where this user can manage the server.

    Owners are included even though Discord doesn't always set the bit for them. Cached per
    user for a minute so navigating the dashboard doesn't spend a rate limit per page.
    """
    now = time.monotonic()
    hit = _GUILD_CACHE.get(user_id)
    if hit is not None and not force and now - hit[1] < GUILD_CACHE_TTL:
        return hit[0]

    guilds = _as_user(token, "/users/@me/guilds")
    allowed = [
        g for g in guilds
        if g.get("owner") or (int(g.get("permissions", 0)) & MANAGE_GUILD) == MANAGE_GUILD
    ]
    _GUILD_CACHE[user_id] = (allowed, now)
    return allowed


def forget_user(user_id: int):
    _GUILD_CACHE.pop(user_id, None)


def guild_channels(guild_id: int) -> list:
    """Text channels the bot can see, for the dropdowns. Uses the bot's own credentials, which
    is correct here: this is about where the bot could post, not about the user."""
    try:
        channels = _as_bot(f"/guilds/{guild_id}/channels")
    except DiscordError:
        return []
    # 0 = text, 5 = announcement. Both are somewhere the bot can post a log or a greeting.
    text = [c for c in channels if c.get("type") in (0, 5)]
    text.sort(key=lambda c: (c.get("position", 0), c.get("name", "")))
    return text


def icon_url(guild: dict) -> str:
    if guild.get("icon"):
        return f"https://cdn.discordapp.com/icons/{guild['id']}/{guild['icon']}.png?size=64"
    return ""


def avatar_url(user: dict) -> str:
    if user.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=64"
    index = (int(user.get("id", 0)) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"
