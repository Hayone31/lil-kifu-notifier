"""Lil Kifu & Lil Nao: Twitch and TikTok notifications for Discord, run by GitHub Actions.

Each run asks Twitch which streamers in config.toml are live, then posts, updates or ends their
messages through a Discord webhook (Lil Kifu). About every 15 minutes it also checks the TikTok
accounts in config.toml and announces new posts through a second webhook (Lil Nao). What it has
seen is remembered in state.json, which the workflow commits back whenever it changes.

Needs TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, DISCORD_WEBHOOK_URL and, for TikTok,
DISCORD_TIKTOK_WEBHOOK_URL in the environment (repository secrets on GitHub, or a .env file next
to this script for local test runs).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
TWITCH_API = "https://api.twitch.tv/helix"
TIKTOK_PROFILE_URL = "https://www.tiktok.com/@{}"
DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com, 1.0) LilKifu"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")

TWITCH_PURPLE = 0x9146FF
ENDED_GRAY = 0x57606A                # border colour of the "ended" embeds in the reference screenshot
TIKTOK_RED = 0xDB4263                # border colour of the TikTok embed in the reference screenshot
MISSES_TO_END = 2                    # checks in a row a stream must be missing before it counts as ended
RESTART_GAP = timedelta(minutes=10)  # a new stream this soon after the last one keeps the same message
TIKTOK_EVERY = 15                    # minutes between TikTok checks; TikTok blocks clients that ask often
SEEN_KEEP = 30                       # video ids remembered per TikTok account
MAX_POSTS_PER_CHECK = 3              # never flood the channel, e.g. after a long outage
KEEPALIVE = timedelta(days=25)       # commit at least this often; GitHub pauses idle schedules after 60 days
UNKNOWN_WEBHOOK, UNKNOWN_MESSAGE = 10015, 10008  # Discord error codes

_LOGIN_RE = re.compile(r"[A-Za-z0-9_]{1,25}")
_TIKTOK_RE = re.compile(r"[A-Za-z0-9_.]{2,24}")
_WEBHOOK_RE = re.compile(
    r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/([\w-]+)/?")


class ConfigError(Exception):
    """Something the repository owner has to fix: settings or secrets."""


class TikTokError(Exception):
    """A TikTok profile couldn't be read."""


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status
        self.body = body

    @property
    def discord_code(self) -> int | None:
        try:
            return json.loads(self.body).get("code")
        except (ValueError, AttributeError):
            return None


# --- HTTP ---------------------------------------------------------------------------------------

def http(method: str, url: str, *, headers: dict | None = None, form: dict | None = None,
         json_body: dict | None = None, text: bool = False):
    """Send a request and return the decoded JSON (or the text). Waits out short 429s."""
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    for attempt in range(3):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
            if text:
                return raw.decode("utf-8", errors="replace")
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            wait = _retry_after(e, body)
            if e.code == 429 and wait is not None and wait <= 10 and attempt < 2:
                time.sleep(wait)
                continue
            raise HttpError(e.code, body, _redact(url)) from None
    raise AssertionError("unreachable")


def _retry_after(error: urllib.error.HTTPError, body: str) -> float | None:
    try:
        return float(json.loads(body)["retry_after"])
    except (ValueError, KeyError, TypeError):
        pass
    try:
        return float(error.headers["Retry-After"])
    except (KeyError, TypeError, ValueError):
        return None


def _redact(url: str) -> str:
    """Never print a webhook token: Actions logs of public repositories are public."""
    return re.sub(r"(/webhooks/\d+/)[^/?]+", r"\1***", url)


# --- Twitch, TikTok and Discord -----------------------------------------------------------------

class Twitch:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        body = http("POST", TWITCH_TOKEN_URL, form={
            "client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"})
        self.token = body["access_token"]

    def _get(self, path: str, params: list[tuple[str, str]]) -> list[dict]:
        url = f"{TWITCH_API}/{path}?{urllib.parse.urlencode(params)}"
        body = http("GET", url, headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self.token}"})
        return body.get("data") or []

    def users(self, logins: list[str]) -> dict[str, dict]:
        logins = sorted(set(logins))
        found = {}
        for start in range(0, len(logins), 100):
            for user in self._get("users", [("login", login) for login in logins[start:start + 100]]):
                found[user["login"]] = user
        return found

    def streams(self, user_ids: set[str]) -> dict[str, dict]:
        """Streams that are live right now, by user id. Offline users are simply missing."""
        ids = sorted(user_ids)
        live = {}
        for start in range(0, len(ids), 100):
            params = [("user_id", user_id) for user_id in ids[start:start + 100]] + [("type", "live"), ("first", "100")]
            for stream in self._get("streams", params):
                live[stream["user_id"]] = stream
        return live

    def stream_end(self, user_id: str, stream_id: str) -> datetime | None:
        """When a finished stream ended, read from its VOD (only works if the streamer keeps VODs)."""
        for video in self._get("videos", [("user_id", user_id), ("type", "archive"), ("first", "5")]):
            if video.get("stream_id") == stream_id:
                return parse_time(video["created_at"]) + parse_duration(video["duration"])
        return None

    def close(self) -> None:
        """Revoke this run's app token, so tokens don't pile up across thousands of runs."""
        try:
            http("POST", TWITCH_REVOKE_URL, form={"client_id": self.client_id, "token": self.token})
        except (HttpError, urllib.error.URLError, TimeoutError):
            pass


def tiktok_profile(handle: str) -> dict:
    """Video count and avatar from a public TikTok profile page.

    TikTok hides the list of videos from data-centre IPs such as GitHub's, but the profile page
    still carries the account's stats, so a growing video count is how new posts are spotted.
    """
    html = http("GET", TIKTOK_PROFILE_URL.format(handle), text=True,
                headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise TikTokError("the profile page has no data (TikTok changed it or blocked the request)")
    detail = json.loads(match.group(1)).get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail") or {}
    if detail.get("statusCode") != 0:
        raise TikTokError(f"TikTok answered with status {detail.get('statusCode')} (account missing or private?)")
    user, stats = detail["userInfo"]["user"], detail["userInfo"]["stats"]
    return {
        "video_count": int(stats["videoCount"]),
        "avatar": user.get("avatarLarger") or user.get("avatarMedium") or "",
    }


class Webhook:
    def __init__(self, url: str, secret_name: str = "DISCORD_WEBHOOK_URL") -> None:
        url = _clean_webhook_url(url)
        match = _WEBHOOK_RE.fullmatch(url)
        if not match:
            raise ConfigError(f"{secret_name} is not a Discord webhook URL: {_webhook_hint(url)}. Copy it again "
                              "from Discord (channel settings > Integrations > Webhooks > Copy Webhook URL)")
        self.secret_name = secret_name
        self.base = f"{DISCORD_API}/webhooks/{match[1]}/{match[2]}"

    def send(self, payload: dict) -> str:
        return http("POST", f"{self.base}?wait=true&with_components=true", json_body=payload)["id"]

    def edit(self, message_id: str, payload: dict) -> None:
        http("PATCH", f"{self.base}/messages/{message_id}?with_components=true", json_body=payload)


def _clean_webhook_url(raw: str) -> str:
    """Forgive common paste mistakes: quotes, a leading NAME=, a query string."""
    url = raw.strip().strip("\"'").strip()
    if "=" in url.split("://", 1)[0]:
        url = url.split("=", 1)[1].strip().strip("\"'").strip()
    return url.split("?", 1)[0].split("#", 1)[0]


def _webhook_hint(url: str) -> str:
    """Say what is wrong without ever echoing the secret itself."""
    if not url:
        return "it is empty"
    if any(char.isspace() for char in url):
        return "it contains spaces or line breaks"
    if not url.startswith("https://"):
        return "it doesn't start with https://"
    if "/api/webhooks/" not in url and "/api/v" not in url:
        return "it isn't a webhook link (those contain /api/webhooks/)"
    return "the part after /api/webhooks/ should be <number>/<token>"


# --- Settings and state -------------------------------------------------------------------------

@dataclass(frozen=True)
class TikTokConfig:
    accounts: tuple[str, ...] = ()  # spelled as in config.toml; that spelling is shown in Discord
    ping: str = ""
    name: str = ""
    avatar_url: str = ""
    api: str = ""  # the panel Worker, which reads TikTok's official API for connected accounts


@dataclass(frozen=True)
class Config:
    streamers: tuple[str, ...]
    ping: str = ""           # "", "everyone" or a role id
    name: str = ""           # overrides the webhook's name when set
    avatar_url: str = ""     # overrides the webhook's avatar when set
    watch_button: bool = True
    tiktok: TikTokConfig = field(default_factory=TikTokConfig)


def parse_login(text: str) -> str | None:
    """Turn 'Name', '@Name' or 'https://twitch.tv/Name' into a login, or None if it can't be one."""
    text = text.strip()
    match = re.search(r"twitch\.tv/(?:popout/)?([A-Za-z0-9_]+)", text, re.IGNORECASE)
    if match:
        text = match.group(1)
    text = text.lstrip("@")
    return text.lower() if _LOGIN_RE.fullmatch(text) else None


def parse_tiktok(text: str) -> str | None:
    """Turn 'Name', '@Name' or a tiktok.com/@Name link into a handle, keeping its spelling."""
    text = text.strip()
    match = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)", text, re.IGNORECASE)
    if match:
        text = match.group(1)
    text = text.lstrip("@")
    return text if _TIKTOK_RE.fullmatch(text) else None


def _parse_ping(value: object, section: str) -> str:
    ping = re.sub(r"[<@&>\s]", "", str(value or "")).lower()
    if ping not in ("", "everyone") and not ping.isdigit():
        raise ConfigError(f'config.toml [{section}]: ping must be "", "everyone" or a role ID')
    return ping


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml has a syntax error: {e}") from None
    twitch = raw.get("twitch", raw)  # older files kept the Twitch settings at the top level
    streamers: list[str] = []
    for item in twitch.get("streamers", []):
        login = parse_login(str(item))
        if login is None:
            raise ConfigError(f"config.toml: {item!r} is not a Twitch username")
        if login not in streamers:
            streamers.append(login)

    tiktok = raw.get("tiktok", {})
    accounts: list[str] = []
    for item in tiktok.get("accounts", []):
        handle = parse_tiktok(str(item))
        if handle is None:
            raise ConfigError(f"config.toml: {item!r} is not a TikTok username")
        if handle.lower() not in {account.lower() for account in accounts}:
            accounts.append(handle)

    return Config(
        streamers=tuple(streamers),
        ping=_parse_ping(twitch.get("ping"), "twitch"),
        name=str(twitch.get("name", "")).strip()[:80],
        avatar_url=str(twitch.get("avatar_url", "")).strip(),
        watch_button=bool(twitch.get("watch_button", True)),
        tiktok=TikTokConfig(
            accounts=tuple(accounts),
            ping=_parse_ping(tiktok.get("ping"), "tiktok"),
            name=str(tiktok.get("name", "")).strip()[:80],
            avatar_url=str(tiktok.get("avatar_url", "")).strip(),
            api=str(tiktok.get("api", "")).strip().rstrip("/"),
        ),
    )


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    state.setdefault("streams", {})
    return state


def save_state(state: dict, path: Path) -> bool:
    """Write state.json. False if nothing changed, so there is nothing to commit."""
    text = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_duration(text: str) -> timedelta:
    """Twitch VOD durations look like '3h8m33s'."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text or "")
    if not text or not match:
        raise ValueError(f"unexpected duration {text!r}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


# --- What the messages look like ----------------------------------------------------------------

def escape(text: str) -> str:
    return re.sub(r"([\\*_~`|>])", r"\\\1", text)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def channel_url(login: str) -> str:
    return f"https://www.twitch.tv/{login}"


def ping_text(ping: str) -> str:
    if not ping:
        return ""
    return "@everyone" if ping == "everyone" else f"<@&{ping}>"


def allowed_mentions(ping: str) -> dict:
    """Only the configured ping may notify anyone; names or titles can never trigger a mention."""
    if ping == "everyone":
        return {"parse": ["everyone"]}
    if ping:
        return {"parse": [], "roles": [ping]}
    return {"parse": []}


def _link_button(label: str, url: str) -> list[dict]:
    return [{"type": 1, "components": [{"type": 2, "style": 5, "label": label, "url": url}]}]


def live_embed(stream: dict, started_at: str, profile: str, now: datetime) -> dict:
    name, login = stream["user_name"], stream["user_login"]
    embed = {
        "author": {"name": _cut(f"{name} is live on Twitch", 256), "url": channel_url(login)},
        "title": _cut((stream.get("title") or "").strip() or "Untitled stream", 256),
        "url": channel_url(login),
        "color": TWITCH_PURPLE,
        "timestamp": started_at,
    }
    if stream.get("game_name"):
        embed["description"] = f"Playing **{escape(stream['game_name'])}**"
    if profile:
        embed["thumbnail"] = {"url": profile}
    if stream.get("thumbnail_url"):
        # The query string makes Discord fetch a fresh preview instead of a cached one.
        preview = stream["thumbnail_url"].replace("{width}", "1280").replace("{height}", "720")
        embed["image"] = {"url": f"{preview}?t={int(now.timestamp())}"}
    return embed


def live_message(config: Config, stream: dict, profile: str, now: datetime) -> dict:
    text = f"**{escape(stream['user_name'])}** is live on Twitch!"
    payload = {
        "content": f"{ping_text(config.ping)} {text}" if config.ping else text,
        "embeds": [live_embed(stream, stream["started_at"], profile, now)],
        "allowed_mentions": allowed_mentions(config.ping),
    }
    if config.watch_button:
        payload["components"] = _link_button("Watch Stream", channel_url(stream["user_login"]))
    if config.name:
        payload["username"] = config.name
    if config.avatar_url:
        payload["avatar_url"] = config.avatar_url
    return payload


def ended_message(name: str, login: str, ended_at: datetime) -> dict:
    return {
        "content": f"**{escape(name)}** was live. The stream has ended.",
        "embeds": [{
            "author": {"name": _cut(f"{name} was live on Twitch", 256)},
            "title": "Stream ended",
            "url": channel_url(login),
            "description": f"{escape(name)}'s stream has ended.",
            "color": ENDED_GRAY,
            "timestamp": iso(ended_at),
        }],
        "components": [],
        "allowed_mentions": {"parse": []},
    }


def tiktok_message(config: TikTokConfig, account: str, avatar: str, new_posts: int) -> dict:
    """Without TikTok's official API: only that something was uploaded, with the profile picture."""
    what = "a new TikTok" if new_posts == 1 else f"{new_posts} new TikToks"
    embed = {"title": _cut(f"{account} uploaded {what}!", 256), "color": TIKTOK_RED}
    if avatar:
        embed["thumbnail"] = {"url": avatar}
    return _tiktok_payload(config, embed, TIKTOK_PROFILE_URL.format(account.lower()))


def tiktok_video_message(config: TikTokConfig, account: str, video: dict) -> dict:
    """With TikTok's official API: the video's caption, cover and a direct link."""
    embed = {"title": _cut(f"{account} uploaded a new TikTok!", 256), "color": TIKTOK_RED}
    caption = (video.get("caption") or "").strip()
    if caption:
        embed["description"] = _cut(escape(caption), 4096)
    if video.get("cover"):
        embed["thumbnail"] = {"url": video["cover"]}
    return _tiktok_payload(config, embed, video.get("url") or TIKTOK_PROFILE_URL.format(account.lower()))


def _tiktok_payload(config: TikTokConfig, embed: dict, link: str) -> dict:
    payload = {
        "embeds": [embed],
        "components": _link_button("View on TikTok", link),
        "allowed_mentions": allowed_mentions(config.ping),
    }
    if config.ping:
        payload["content"] = ping_text(config.ping)
    if config.name:
        payload["username"] = config.name
    if config.avatar_url:
        payload["avatar_url"] = config.avatar_url
    return payload


# --- Twitch check -------------------------------------------------------------------------------

def check(config: Config, state: dict, twitch: Twitch, webhook: Webhook, now: datetime) -> list[str]:
    """Compare who is live with state.json and update Discord. Returns a line per visible change."""
    changes: list[str] = []
    entries: dict[str, dict] = state["streams"]
    users = twitch.users(list(config.streamers)) if config.streamers else {}
    for login in config.streamers:
        if login not in users:
            warn(f"There's no Twitch user called {login!r} (check config.toml)")
    tracked = {user["id"]: user for user in users.values()}
    ids = set(tracked) | set(entries)  # removed streamers stay watched until their live message is ended
    live = twitch.streams(ids) if ids else {}

    for user_id in sorted(ids):
        stream, entry, user = live.get(user_id), entries.get(user_id), tracked.get(user_id)
        name = (stream or {}).get("user_name") or (entry or {}).get("name") or (user or {}).get("display_name")
        try:
            if stream is None:
                if entry is not None:
                    # Twitch hiccups and stream crashes make streams vanish briefly; wait for a second miss.
                    entry["misses"] = entry.get("misses", 0) + 1
                    entry["missing_since"] = entry.get("missing_since") or iso(now)
                    if entry["misses"] >= MISSES_TO_END:
                        end_message(twitch, webhook, user_id, entry, now)
                        del entries[user_id]
                        changes.append(f"{name}'s stream ended")
            elif entry is None:
                if user is not None:
                    entries[user_id] = post_message(config, webhook, stream, user, now)
                    changes.append(f"{name} went live")
            elif stream["id"] == entry["stream_id"] or is_restart(twitch, user_id, entry, stream):
                if update_message(webhook, entry, stream, now):
                    changes.append(f"{name} changed title or game")
            else:
                end_message(twitch, webhook, user_id, entry, now)
                del entries[user_id]
                changes.append(f"{name}'s stream ended")
                if user is not None:
                    entries[user_id] = post_message(config, webhook, stream, user, now)
                    changes.append(f"{name} went live")
        except HttpError as e:
            _raise_if_webhook_gone(e, webhook)
            warn(f"{name}: {e} (trying again next run)")
        except (urllib.error.URLError, TimeoutError) as e:
            warn(f"{name}: {e} (trying again next run)")
    return changes


def post_message(config: Config, webhook: Webhook, stream: dict, user: dict, now: datetime) -> dict:
    profile = user.get("profile_image_url") or ""
    payload = live_message(config, stream, profile, now)
    try:
        message_id = webhook.send(payload)
    except HttpError as e:
        if e.status != 400 or "components" not in payload:
            raise
        warn("Discord refused the Watch Stream button; posting without it")
        del payload["components"]
        message_id = webhook.send(payload)
    return {
        "message_id": message_id,
        "stream_id": stream["id"],
        "login": stream["user_login"],
        "name": stream["user_name"],
        "title": stream.get("title") or "",
        "game": stream.get("game_name") or "",
        "profile": profile,
        "started_at": stream["started_at"],
        "misses": 0,
        "missing_since": None,
    }


def update_message(webhook: Webhook, entry: dict, stream: dict, now: datetime) -> bool:
    """Keep a live message in sync with the stream. True if the message was edited."""
    current = (stream.get("title") or "", stream.get("game_name") or "", stream["user_name"])
    changed = current != (entry["title"], entry["game"], entry["name"])
    if changed and entry.get("message_id"):
        try:
            webhook.edit(entry["message_id"], {"embeds": [live_embed(stream, entry["started_at"], entry["profile"], now)]})
        except HttpError as e:
            if e.discord_code != UNKNOWN_MESSAGE:
                raise
            entry["message_id"] = None  # someone deleted it: leave the stream alone until it ends
    entry.update(stream_id=stream["id"], login=stream["user_login"], name=current[2], title=current[0],
                 game=current[1], misses=0, missing_since=None)
    return changed


def end_message(twitch: Twitch, webhook: Webhook, user_id: str, entry: dict, now: datetime) -> None:
    ended_at = _stream_end(twitch, user_id, entry["stream_id"])
    if ended_at is None:
        ended_at = parse_time(entry["missing_since"]) if entry.get("missing_since") else now
    if entry.get("message_id"):
        try:
            webhook.edit(entry["message_id"], ended_message(entry["name"], entry["login"], ended_at))
        except HttpError as e:
            if e.discord_code != UNKNOWN_MESSAGE:
                raise


def is_restart(twitch: Twitch, user_id: str, entry: dict, stream: dict) -> bool:
    """A new stream id: a quick restart keeps the old message, a later new stream gets a new one."""
    if entry.get("misses"):
        return True  # it was gone for a check, so this is the reconnect
    old_end = _stream_end(twitch, user_id, entry["stream_id"])
    return old_end is None or parse_time(stream["started_at"]) - old_end <= RESTART_GAP


def _stream_end(twitch: Twitch, user_id: str, stream_id: str) -> datetime | None:
    try:
        return twitch.stream_end(user_id, stream_id)
    except (HttpError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return None


def _raise_if_webhook_gone(error: HttpError, webhook: Webhook) -> None:
    if error.discord_code == UNKNOWN_WEBHOOK or error.status == 401:
        raise ConfigError(f"Discord doesn't accept the webhook anymore. Create a new one and update the "
                          f"{webhook.secret_name} secret") from None


# --- TikTok check -------------------------------------------------------------------------------

def tiktok_due(now: datetime) -> bool:
    """TikTok is looked at during the first five minutes of every quarter hour."""
    return now.minute % TIKTOK_EVERY < 5


def tiktok_videos(api: str, handle: str) -> list[dict] | None:
    """Latest videos from the panel Worker, or None when the account isn't connected to TikTok's API."""
    try:
        body = http("GET", f"{api}/tiktok/videos?{urllib.parse.urlencode({'account': handle})}")
    except HttpError as e:
        if e.status == 404:
            return None
        raise
    return body.get("videos") or []


def announce_videos(config: TikTokConfig, entry: dict, account: str, videos: list[dict], webhook: Webhook) -> list[str]:
    """Post the videos that weren't there last time. The first look only remembers what is there."""
    entry.pop("video_count", None)  # if the API goes away, counting starts fresh instead of re-announcing
    ids = [video["id"] for video in videos]
    seen = entry.get("seen")
    if seen is None:
        entry["seen"] = ids[:SEEN_KEEP]
        return []
    new = sorted((video for video in videos if video["id"] not in seen), key=lambda video: video.get("created") or 0)
    failed: set[str] = set()
    changes = []
    for video in new[-MAX_POSTS_PER_CHECK:]:  # older extras count as seen without a post
        try:
            webhook.send(tiktok_video_message(config, account, video))
        except HttpError as e:
            _raise_if_webhook_gone(e, webhook)
            warn(f"TikTok @{account}: {e} (trying again later)")
            failed.add(video["id"])
            continue
        changes.append(f"{account} uploaded a new TikTok")
    entry["seen"] = ([i for i in ids if i not in failed] + [i for i in seen if i not in ids])[:SEEN_KEEP]
    return changes


def check_tiktok(config: TikTokConfig, state: dict, webhook: Webhook, now: datetime, *,
                 force: bool = False, fetch=tiktok_profile, fetch_videos=tiktok_videos) -> list[str]:
    """Announce new TikTok posts. Accounts are looked at about every TIKTOK_EVERY minutes.

    Accounts connected to TikTok's official API (through the panel Worker) get the video's caption,
    cover and link. The others are watched through the video count on their public profile page.
    """
    entries: dict[str, dict] = state.setdefault("tiktok", {})
    wanted = {account.lower(): account for account in config.accounts}
    for handle in list(entries):
        if handle not in wanted:
            del entries[handle]
    due = force or tiktok_due(now)
    changes: list[str] = []
    for handle, account in wanted.items():
        if not due and handle in entries:
            continue
        if config.api:
            try:
                videos = fetch_videos(config.api, handle)
            except (HttpError, urllib.error.URLError, TimeoutError, ValueError) as e:
                warn(f"TikTok @{handle}: the official API isn't answering ({e}); counting videos instead")
                videos = None
            if videos is not None:
                changes += announce_videos(config, entries.setdefault(handle, {}), account, videos, webhook)
                continue
        try:
            profile = fetch(handle)
        except (HttpError, urllib.error.URLError, TimeoutError, TikTokError, ValueError, KeyError) as e:
            warn(f"TikTok @{handle}: {e} (trying again later)")
            continue
        entry = entries.get(handle)
        if entry is None or "video_count" not in entry:  # first look: remember where it stands, announce nothing
            entries[handle] = {"video_count": profile["video_count"]}
            continue
        new_posts = profile["video_count"] - entry["video_count"]
        if new_posts > 0:
            try:
                webhook.send(tiktok_message(config, account, profile["avatar"], new_posts))
            except HttpError as e:
                _raise_if_webhook_gone(e, webhook)
                warn(f"TikTok @{handle}: {e} (trying again later)")
                continue  # the count stays as it was, so the next check announces it
            changes.append(f"{account} uploaded a new TikTok")
        entry["video_count"] = profile["video_count"]  # also follows deleted videos down
    return changes


# --- Entry point --------------------------------------------------------------------------------

def warn(message: str) -> None:
    print(f"::warning::{message}")


def load_dotenv(path: Path) -> None:
    """Local test runs: read KEY=value lines from .env. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set (Settings > Secrets and variables > Actions)")
    return value


def run_twitch(config: Config, state: dict, now: datetime) -> list[str]:
    """Raises ConfigError for things the owner must fix; outages only produce warnings."""
    webhook = Webhook(required_env("DISCORD_WEBHOOK_URL"))
    client_id, client_secret = required_env("TWITCH_CLIENT_ID"), required_env("TWITCH_CLIENT_SECRET")
    try:
        twitch = Twitch(client_id, client_secret)
    except HttpError as e:
        if e.status in (400, 401, 403):
            raise ConfigError("Twitch rejected TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET") from None
        warn(f"Twitch is unreachable right now ({e}); trying again next run")
        return []
    except (urllib.error.URLError, TimeoutError) as e:
        warn(f"Twitch is unreachable right now ({e}); trying again next run")
        return []
    try:
        return check(config, state, twitch, webhook, now)
    except (HttpError, urllib.error.URLError, TimeoutError) as e:
        # Nothing was posted yet when the Twitch lookups fail, so it's safe to just try again later.
        warn(f"Couldn't ask Twitch who is live ({e}); trying again next run")
        return []
    finally:
        twitch.close()


def run_tiktok(config: Config, state: dict, now: datetime, force: bool) -> list[str]:
    if not config.tiktok.accounts:
        state.pop("tiktok", None)
        return []
    url = os.environ.get("DISCORD_TIKTOK_WEBHOOK_URL", "").strip()
    if not url:
        warn("DISCORD_TIKTOK_WEBHOOK_URL is not set, so TikTok accounts are skipped")
        return []
    webhook = Webhook(url, "DISCORD_TIKTOK_WEBHOOK_URL")
    return check_tiktok(config.tiktok, state, webhook, now, force=force)


def report(config: Config, state: dict, changes: list[str], changed: bool) -> None:
    for line in changes:
        print(line)
    summary = ("; ".join(changes) or "Update state").replace("\n", " ")[:200]
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\nsummary={summary}\n")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        live = {entry["login"] for entry in state["streams"].values()}
        rows = [f"| {login} | {'🔴 live' if login in live else 'offline'} |" for login in config.streamers]
        for account in config.tiktok.accounts:
            entry = state.get("tiktok", {}).get(account.lower(), {})
            status = "official API" if "seen" in entry else f"{entry.get('video_count', '?')} videos"
            rows.append(f"| TikTok @{account} | {status} |")
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n".join(["| Account | Status |", "|---|---|", *rows]) + "\n")


def main() -> int:
    load_dotenv(ROOT / ".env")
    now = datetime.now(timezone.utc)
    state_path = ROOT / "state.json"
    try:
        config = load_config()
    except ConfigError as e:
        print(f"::error::{e}")
        return 1
    state = load_state(state_path)
    exit_code = 0
    changes: list[str] = []
    # Checks started by hand or by a config change look at TikTok right away; the 5-minute timer doesn't.
    source = os.environ.get("RUN_SOURCE") or os.environ.get("GITHUB_EVENT_NAME", "")
    force_tiktok = source in ("manual", "push", "workflow_dispatch")
    for run in (lambda: run_twitch(config, state, now), lambda: run_tiktok(config, state, now, force_tiktok)):
        try:
            changes += run()
        except ConfigError as e:
            print(f"::error::{e}")
            exit_code = 1

    if not state.get("keepalive") or now - parse_time(state["keepalive"]) >= KEEPALIVE:
        state["keepalive"] = iso(now)
    report(config, state, changes, save_state(state, state_path))
    return exit_code


def say(text: str, voice: str) -> int:
    """Post a plain message through one of the webhooks (the "Say something" workflow)."""
    load_dotenv(ROOT / ".env")
    try:
        config = load_config()
        if voice == "twitch":
            secret, name, avatar = "DISCORD_WEBHOOK_URL", config.name, config.avatar_url
        else:
            secret, name, avatar = "DISCORD_TIKTOK_WEBHOOK_URL", config.tiktok.name, config.tiktok.avatar_url
        webhook = Webhook(required_env(secret), secret)
    except ConfigError as e:
        print(f"::error::{e}")
        return 1
    payload = {"content": text[:2000], "allowed_mentions": {"parse": []}}  # never pings anyone
    if name:
        payload["username"] = name
    if avatar:
        payload["avatar_url"] = avatar
    try:
        webhook.send(payload)
    except (HttpError, urllib.error.URLError, TimeoutError) as e:
        print(f"::error::Discord didn't take the message: {e}")
        return 1
    print(f"Posted as {name or 'the webhook'}")
    return 0


if __name__ == "__main__":
    if text := os.environ.get("SAY_TEXT", "").strip():
        sys.exit(say(text, "twitch" if "twitch" in os.environ.get("SAY_AS", "").lower() else "tiktok"))
    sys.exit(main())
