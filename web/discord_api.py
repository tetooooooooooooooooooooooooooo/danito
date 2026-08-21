"""Talking to Discord: the OAuth dance for logging people in, and the few REST calls the
dashboard needs.

Two different credentials are in play and they must not be confused. The *user's* OAuth token
says who is logged in and which servers they belong to. The *bot's* token is what lists a
guild's channels. Never use the bot token to decide what a user is allowed to see.
"""

import base64
import os
import re
import time

import requests

API = "https://discord.com/api/v10"
AUTHORIZE = "https://discord.com/oauth2/authorize"
TOKEN = f"{API}/oauth2/token"
SCOPES = "identify guilds"
TIMEOUT = 10

MANAGE_GUILD = 0x20          # the permission bit that makes somebody a server admin here

def _env(name: str) -> str:
    """Stripped, because a config var pasted with a trailing newline or space is invisible in
    a dashboard and produces errors that point nowhere near the cause."""
    return (os.environ.get(name) or "").strip().strip('"').strip("'")


CLIENT_ID = _env("DISCORD_CLIENT_ID")
CLIENT_SECRET = _env("DISCORD_CLIENT_SECRET")
REDIRECT_URI = _env("DISCORD_REDIRECT_URI")
BOT_TOKEN = _env("BOT_TOKEN")

# Discord's guild list is rate limited, and a user reloading the picker shouldn't cost a call
# each time. Short enough that leaving a server is reflected quickly.
_GUILD_CACHE: dict[int, tuple[list, float]] = {}
GUILD_CACHE_TTL = 60


class DiscordError(RuntimeError):
    pass


def configured() -> list:
    """What's wrong with the setup, in plain terms, so the app can say so itself.

    Catching a malformed redirect here matters: handing it to Discord produces
    "redirect_uri is not a well formed url", which names the symptom and gives no clue
    which value is at fault or what it currently contains.
    """
    problems = []
    for name, value in (("DISCORD_CLIENT_ID", CLIENT_ID),
                        ("DISCORD_CLIENT_SECRET", CLIENT_SECRET),
                        ("DISCORD_REDIRECT_URI", REDIRECT_URI),
                        ("BOT_TOKEN", BOT_TOKEN)):
        if not value:
            problems.append(f"{name} is not set.")

    if REDIRECT_URI:
        if not REDIRECT_URI.startswith(("http://", "https://")):
            problems.append(
                f"DISCORD_REDIRECT_URI must start with https:// (or http:// for localhost). "
                f"It is currently {REDIRECT_URI!r}.")
        elif not REDIRECT_URI.rstrip("/").endswith("/callback"):
            problems.append(
                f"DISCORD_REDIRECT_URI must end with /callback, which is the route that "
                f"receives the login. It is currently {REDIRECT_URI!r}.")
        elif " " in REDIRECT_URI:
            problems.append(f"DISCORD_REDIRECT_URI contains a space: {REDIRECT_URI!r}.")

    if CLIENT_ID and not CLIENT_ID.isdigit():
        problems.append(
            f"DISCORD_CLIENT_ID should be only digits. It is currently {CLIENT_ID!r}, which "
            f"is usually the client secret pasted into the wrong variable.")

    return problems


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
    is correct here: this is about where the bot could post, not about the user.

    Raises DiscordError rather than returning an empty list when the call fails. Those are two
    different facts and the caller has to be able to tell them apart: an empty list means the
    bot really cannot see any channels, which is a permission to go and fix, while a failure
    means Discord did not answer and nothing at all is known. Returning [] for both made the
    settings page blame a permission for a rate limit, and made a save silently drop every
    channel it could not validate.
    """
    channels = _as_bot(f"/guilds/{guild_id}/channels")
    # 0 = text, 5 = announcement. Both are somewhere the bot can post a log or a greeting.
    text = [c for c in channels if c.get("type") in (0, 5)]
    text.sort(key=lambda c: (c.get("position", 0), c.get("name", "")))
    return text


def guild_roles(guild_id: int) -> list:
    """The guild's roles, each carrying whether the bot could actually hand it out.

    Working this out here rather than letting the save fail is the point. Discord refuses an
    assignment with a bare 403, so a dashboard that offers every role produces a setting that
    looks saved and silently never fires. The two rules it enforces are that managed roles
    belong to an integration, and that a bot cannot grant a role at or above its own highest.
    Raises on failure for the same reason as guild_channels above: no roles and no answer are
    different facts, and a save that treats the second as the first quietly discards
    everything it was asked to store.
    """
    roles = _as_bot(f"/guilds/{guild_id}/roles")

    by_id = {r["id"]: r for r in roles}

    # The bot's own highest position. Unknown (-1) means we couldn't read our membership, in
    # which case say nothing about hierarchy rather than guess and grey out everything.
    top = -1
    try:
        me = _as_bot(f"/guilds/{guild_id}/members/{CLIENT_ID}")
        for role_id in me.get("roles", []):
            held = by_id.get(role_id)
            if held:
                top = max(top, held.get("position", 0))
    except DiscordError:
        pass

    out = []
    for role in roles:
        # @everyone always shares the guild's own id, and nobody needs to be given it.
        if role.get("id") == str(guild_id):
            continue
        position = role.get("position", 0)
        if role.get("managed"):
            problem = "managed by an integration, so Discord won't let anyone assign it"
        elif top >= 0 and position >= top:
            problem = "sits above my highest role, so I can't hand it out"
        else:
            problem = None
        colour = role.get("color") or 0
        out.append({
            "id": role["id"],
            "name": role.get("name", "unnamed"),
            "position": position,
            "problem": problem,
            "colour": f"#{colour:06x}" if colour else "",
        })

    out.sort(key=lambda r: r["position"], reverse=True)
    return out


def _explain(payload) -> str:
    """Flatten Discord's nested validation errors into something a person can act on.

    It answers a rejected message with a tree keyed by the path to each bad field, which is
    genuinely the most useful error in the whole API and completely unreadable raw. Turning
    `{"embeds": {"0": {"image": {"url": {"_errors": [...]}}}}}` into "embeds.0.image.url: Not
    a well formed URL" is the difference between fixing it and guessing.
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            if "_errors" in node:
                for problem in node["_errors"]:
                    where = ".".join(path)
                    found.append(f"{where}: {problem.get('message', '?')}" if where
                                 else problem.get("message", "?"))
                return
            for key, value in node.items():
                walk(value, path + [key])

    if isinstance(payload, dict):
        walk(payload.get("errors") or {}, [])
        if not found and payload.get("message"):
            found.append(payload["message"])
    return "; ".join(found[:6])


def post_message(channel_id: int, payload: dict) -> dict:
    """Send a message as the bot.

    The dashboard has no gateway connection, but it does not need one to post: this is an
    ordinary REST call with the bot's own token, the same credential already used to list a
    guild's channels. Role panels go the long way round through the bot because their buttons
    have to be handled by something with a gateway; a one-off message has no such problem.
    """
    resp = requests.post(
        f"{API}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"},
        json=payload, timeout=TIMEOUT)
    if resp.status_code in (200, 201):
        return resp.json()

    try:
        detail = _explain(resp.json())
    except ValueError:
        detail = ""
    if resp.status_code == 403:
        raise DiscordError("I can't post in that channel. Check I have View Channel, "
                           "Send Messages and Embed Links there.")
    if resp.status_code == 404:
        raise DiscordError("That channel doesn't exist any more.")
    raise DiscordError(detail or f"Discord returned {resp.status_code}")


def icon_url(guild: dict) -> str:
    if guild.get("icon"):
        return f"https://cdn.discordapp.com/icons/{guild['id']}/{guild['icon']}.png?size=64"
    return ""


def avatar_url(user: dict) -> str:
    if user.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=64"
    index = (int(user.get("id", 0)) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


# ── soundboard ───────────────────────────────────────────────────────
# Discord's limits, mirrored here so the dashboard can refuse a file before spending a round
# trip on it. A sound is mp3 or ogg, at most 512KB and 5.2 seconds.
SOUND_MAX_BYTES = 512 * 1024
SOUND_MAX_SECONDS = 5.2
SOUND_TYPES = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg"}
SOUND_NAME_MIN, SOUND_NAME_MAX = 2, 32
SOUND_CDN = "https://cdn.discordapp.com/soundboard-sounds"


def _sound_error(resp) -> str:
    try:
        detail = _explain(resp.json())
    except ValueError:
        detail = ""
    if resp.status_code == 403:
        # Manage Expressions is not enough. Discord wants Create Expressions to add one, and
        # it is not in the invite by default, so this is the failure a server hits first.
        raise DiscordError(
            "Discord refused that. I need both Create Expressions and Manage Expressions in "
            "this server, and adding the bot again is what grants them.")
    if resp.status_code == 404:
        raise DiscordError("That sound doesn't exist any more.")
    if resp.status_code == 429:
        raise DiscordError("Discord is rate limiting soundboard changes. Wait a moment.")
    raise DiscordError(detail or f"Discord returned {resp.status_code}")


def guild_sounds(guild_id: int) -> list:
    """Every soundboard sound in the guild, in the order Discord returns them.

    That order is the only ordering there is: a sound carries no position field, so what comes
    back here is what members see. Reordering therefore means recreating them, which is why
    the dashboard makes such a noise about it.
    """
    data = _as_bot(f"/guilds/{guild_id}/soundboard-sounds")
    items = data.get("items", []) if isinstance(data, dict) else data
    return items or []


def create_sound(guild_id: int, name: str, raw: bytes, content_type: str,
                 volume: float = 1.0, emoji: str = None) -> dict:
    """Upload a sound. Returns the created sound, which has a new id.

    The file travels as a data URI inside JSON, the same way icons and emoji do, rather than
    as multipart.
    """
    kind = SOUND_TYPES.get((content_type or "").lower().split(";")[0].strip())
    if kind is None:
        raise DiscordError("Discord only takes mp3 and ogg files.")
    if len(raw) > SOUND_MAX_BYTES:
        raise DiscordError(f"That file is {len(raw) // 1024}KB. The limit is 512KB.")

    mime = "audio/mpeg" if kind == "mp3" else "audio/ogg"
    body = {
        "name": name,
        "sound": f"data:{mime};base64,{base64.b64encode(raw).decode()}",
        "volume": max(0.0, min(1.0, float(volume))),
    }
    if emoji:
        # A custom emoji arrives as <:name:id>; anything else is taken as a unicode one.
        found = re.fullmatch(r"<a?:([A-Za-z0-9_]{2,32}):(\d+)>", emoji.strip())
        if found:
            body["emoji_id"] = found.group(2)
        else:
            body["emoji_name"] = emoji.strip()[:32]

    resp = requests.post(f"{API}/guilds/{guild_id}/soundboard-sounds",
                         headers={"Authorization": f"Bot {BOT_TOKEN}",
                                  "Content-Type": "application/json"},
                         json=body, timeout=TIMEOUT * 3)
    if resp.status_code in (200, 201):
        return resp.json()
    _sound_error(resp)


def edit_sound(guild_id: int, sound_id: int, **fields) -> dict:
    """Change a sound's name, volume or emoji. The id survives, so favourites survive."""
    body = {k: v for k, v in fields.items() if v is not None}
    if not body:
        return {}
    resp = requests.patch(f"{API}/guilds/{guild_id}/soundboard-sounds/{sound_id}",
                          headers={"Authorization": f"Bot {BOT_TOKEN}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=TIMEOUT)
    if resp.status_code == 200:
        return resp.json()
    _sound_error(resp)


def delete_sound(guild_id: int, sound_id: int):
    resp = requests.delete(f"{API}/guilds/{guild_id}/soundboard-sounds/{sound_id}",
                           headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=TIMEOUT)
    if resp.status_code in (200, 204):
        return
    _sound_error(resp)


def sound_bytes(sound_id: int) -> bytes:
    """The original file, off the CDN. Needed to put a sound back after deleting it."""
    resp = requests.get(f"{SOUND_CDN}/{sound_id}", timeout=TIMEOUT * 3)
    if resp.status_code != 200:
        raise DiscordError(f"Couldn't download sound {sound_id} to move it.")
    return resp.content
