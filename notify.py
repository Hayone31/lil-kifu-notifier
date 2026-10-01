"""Lil Kifu: Twitch go-live notifications for Discord, run by GitHub Actions.

Each run asks Twitch which streamers in config.toml are live, then posts, updates or ends their
messages through a Discord webhook. Open live messages are remembered in state.json, which the
workflow commits back to the repository whenever it changes.

Needs TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET and DISCORD_WEBHOOK_URL in the environment
(repository secrets on GitHub, or a .env file next to this script for local test runs).
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
TWITCH_API = "https://api.twitch.tv/helix"
DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com, 1.0) LilKifu"

TWITCH_PURPLE = 0x9146FF
ENDED_GRAY = 0x57606A                # border colour of the "ended" embeds in the reference screenshot
MISSES_TO_END = 2                    # checks in a row a stream must be missing before it counts as ended
RESTART_GAP = timedelta(minutes=10)  # a new stream this soon after the last one keeps the same message
KEEPALIVE = timedelta(days=25)       # commit at least this often; GitHub pauses idle schedules after 60 days
UNKNOWN_WEBHOOK, UNKNOWN_MESSAGE = 10015, 10008  # Discord error codes

_LOGIN_RE = re.compile(r"[A-Za-z0-9_]{1,25}")
_WEBHOOK_RE = re.compile(
    r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/([\w-]+)/?")


class ConfigError(Exception):
    """Something the repository owner has to fix: settings or secrets."""


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
         json_body: dict | None = None):
    """Send a request and return the decoded JSON (None for an empty body). Waits out short 429s."""
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


# --- Twitch and Discord -------------------------------------------------------------------------

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


class Webhook:
    def __init__(self, url: str) -> None:
        match = _WEBHOOK_RE.fullmatch(url.strip())
        if not match:
            raise ConfigError("DISCORD_WEBHOOK_URL is not a Discord webhook URL")
        self.base = f"{DISCORD_API}/webhooks/{match[1]}/{match[2]}"

    def send(self, payload: dict) -> str:
        return http("POST", f"{self.base}?wait=true&with_components=true", json_body=payload)["id"]

    def edit(self, message_id: str, payload: dict) -> None:
        http("PATCH", f"{self.base}/messages/{message_id}?with_components=true", json_body=payload)


# --- Settings and state -------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    streamers: tuple[str, ...]
    ping: str = ""           # "", "everyone" or a role id
    name: str = ""           # overrides the webhook's name when set
    avatar_url: str = ""     # overrides the webhook's avatar when set
    watch_button: bool = True


def parse_login(text: str) -> str | None:
    """Turn 'Name', '@Name' or 'https://twitch.tv/Name' into a login, or None if it can't be one."""
    text = text.strip()
    match = re.search(r"twitch\.tv/(?:popout/)?([A-Za-z0-9_]+)", text, re.IGNORECASE)
    if match:
        text = match.group(1)
    text = text.lstrip("@")
    return text.lower() if _LOGIN_RE.fullmatch(text) else None


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml has a syntax error: {e}") from None
    streamers: list[str] = []
    for item in raw.get("streamers", []):
        login = parse_login(str(item))
        if login is None:
            raise ConfigError(f"config.toml: {item!r} is not a Twitch username")
        if login not in streamers:
            streamers.append(login)
    ping = re.sub(r"[<@&>\s]", "", str(raw.get("ping", ""))).lower()
    if ping not in ("", "everyone") and not ping.isdigit():
        raise ConfigError('config.toml: ping must be "", "everyone" or a role ID')
    return Config(
        streamers=tuple(streamers),
        ping=ping,
        name=str(raw.get("name", "")).strip()[:80],
        avatar_url=str(raw.get("avatar_url", "")).strip(),
        watch_button=bool(raw.get("watch_button", True)),
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
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "Watch Stream", "url": channel_url(stream["user_login"])}]}]
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


# --- One check ----------------------------------------------------------------------------------

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
            if e.discord_code == UNKNOWN_WEBHOOK or e.status == 401:
                raise ConfigError("Discord doesn't accept the webhook anymore. Create a new one and "
                                  "update the DISCORD_WEBHOOK_URL secret") from None
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
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n".join(["| Streamer | Status |", "|---|---|", *rows]) + "\n")


def main() -> int:
    load_dotenv(ROOT / ".env")
    now = datetime.now(timezone.utc)
    state_path = ROOT / "state.json"
    try:
        config = load_config()
        webhook = Webhook(required_env("DISCORD_WEBHOOK_URL"))
        client_id, client_secret = required_env("TWITCH_CLIENT_ID"), required_env("TWITCH_CLIENT_SECRET")
    except ConfigError as e:
        print(f"::error::{e}")
        return 1
    state = load_state(state_path)

    try:
        twitch = Twitch(client_id, client_secret)
    except HttpError as e:
        if e.status in (400, 401, 403):
            print("::error::Twitch rejected TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET")
            return 1
        warn(f"Twitch is unreachable right now ({e}); trying again next run")
        return 0
    except (urllib.error.URLError, TimeoutError) as e:
        warn(f"Twitch is unreachable right now ({e}); trying again next run")
        return 0

    try:
        changes = check(config, state, twitch, webhook, now)
    except ConfigError as e:
        print(f"::error::{e}")
        return 1
    except (HttpError, urllib.error.URLError, TimeoutError) as e:
        # Nothing was posted yet when the Twitch lookups fail, so it's safe to just try again later.
        warn(f"Couldn't ask Twitch who is live ({e}); trying again next run")
        return 0
    finally:
        twitch.close()

    if not state.get("keepalive") or now - parse_time(state["keepalive"]) >= KEEPALIVE:
        state["keepalive"] = iso(now)
    report(config, state, changes, save_state(state, state_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
