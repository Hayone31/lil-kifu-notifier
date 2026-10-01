import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import notify
from notify import Config, ConfigError, HttpError

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CHECK = timedelta(minutes=5)
CAROL = {"id": "1", "login": "carolinawwm", "display_name": "CarolinaWWM",
         "profile_image_url": "https://cdn/pfp.png"}


def stream(stream_id="s1", title="Ranked grind", game="VALORANT", started_at="2026-10-01T11:55:00Z"):
    return {"id": stream_id, "user_id": "1", "user_login": "carolinawwm", "user_name": "CarolinaWWM",
            "title": title, "game_name": game, "started_at": started_at,
            "thumbnail_url": "https://static-cdn.jtvnw.net/previews-ttv/live_user_carolinawwm-{width}x{height}.jpg"}


class FakeTwitch:
    def __init__(self):
        self.users_by_login = {"carolinawwm": CAROL}
        self.live = {}
        self.vod_ends = {}

    def users(self, logins):
        return {login: user for login, user in self.users_by_login.items() if login in logins}

    def streams(self, ids):
        return {user_id: s for user_id, s in self.live.items() if user_id in ids}

    def stream_end(self, user_id, stream_id):
        return self.vod_ends.get(stream_id)


class FakeWebhook:
    def __init__(self):
        self.sent, self.edits = [], []
        self.fail_send = self.fail_edit = None
        self._next_id = 100

    def send(self, payload):
        if self.fail_send:
            error, self.fail_send = self.fail_send, None
            raise error
        self._next_id += 1
        self.sent.append(payload)
        return str(self._next_id)

    def edit(self, message_id, payload):
        if self.fail_edit:
            raise self.fail_edit
        self.edits.append((message_id, payload))


@pytest.fixture
def env():
    twitch, webhook, state = FakeTwitch(), FakeWebhook(), {"streams": {}}
    config = Config(streamers=("carolinawwm",), name="Lil Kifu")

    def run(at, cfg=None):
        return notify.check(cfg or config, state, twitch, webhook, at)

    return SimpleNamespace(twitch=twitch, webhook=webhook, state=state, run=run)


def discord_error(status, code):
    return HttpError(status, json.dumps({"message": "x", "code": code}), "https://discord.com/api/webhooks/1/***")


# --- the stream lifecycle -----------------------------------------------------------------------

def test_full_stream_lifecycle(env):
    env.twitch.live = {"1": stream()}
    assert env.run(T0) == ["CarolinaWWM went live"]
    [sent] = env.webhook.sent
    assert sent["content"] == "**CarolinaWWM** is live on Twitch!"
    assert sent["username"] == "Lil Kifu"
    assert sent["allowed_mentions"] == {"parse": []}
    embed = sent["embeds"][0]
    assert embed["author"] == {"name": "CarolinaWWM is live on Twitch", "url": "https://www.twitch.tv/carolinawwm"}
    assert (embed["title"], embed["description"]) == ("Ranked grind", "Playing **VALORANT**")
    assert embed["thumbnail"] == {"url": "https://cdn/pfp.png"}
    assert embed["image"]["url"] == (
        f"https://static-cdn.jtvnw.net/previews-ttv/live_user_carolinawwm-1280x720.jpg?t={int(T0.timestamp())}")
    assert (embed["color"], embed["timestamp"]) == (0x9146FF, "2026-10-01T11:55:00Z")
    assert sent["components"][0]["components"][0] == {
        "type": 2, "style": 5, "label": "Watch Stream", "url": "https://www.twitch.tv/carolinawwm"}

    # still live, nothing changed: no Discord calls and no state change (so no commit)
    before = json.dumps(env.state, sort_keys=True)
    assert env.run(T0 + CHECK) == []
    assert env.webhook.edits == [] and json.dumps(env.state, sort_keys=True) == before

    # title changed: only the embed is edited, the content (and its ping) stays
    env.twitch.live = {"1": stream(title="Road to Radiant")}
    assert env.run(T0 + 2 * CHECK) == ["CarolinaWWM changed title or game"]
    [(message_id, payload)] = env.webhook.edits
    assert message_id == "101" and set(payload) == {"embeds"}
    assert payload["embeds"][0]["title"] == "Road to Radiant"
    assert payload["embeds"][0]["timestamp"] == "2026-10-01T11:55:00Z"

    # offline: the first miss waits, the second ends it with the end time from the VOD
    env.twitch.live = {}
    env.twitch.vod_ends = {"s1": datetime(2026, 10, 1, 12, 13, 7, tzinfo=timezone.utc)}
    assert env.run(T0 + 3 * CHECK) == []
    assert env.state["streams"]["1"]["misses"] == 1
    assert env.run(T0 + 4 * CHECK) == ["CarolinaWWM's stream ended"]
    message_id, ended = env.webhook.edits[-1]
    assert message_id == "101"
    assert ended["content"] == "**CarolinaWWM** was live. The stream has ended."
    assert ended["embeds"] == [{
        "author": {"name": "CarolinaWWM was live on Twitch"},
        "title": "Stream ended",
        "url": "https://www.twitch.tv/carolinawwm",
        "description": "CarolinaWWM's stream has ended.",
        "color": 0x57606A,
        "timestamp": "2026-10-01T12:13:07+00:00",
    }]
    assert ended["components"] == [] and ended["allowed_mentions"] == {"parse": []}
    assert env.state["streams"] == {}
    assert len(env.webhook.sent) == 1


def test_end_time_falls_back_to_the_first_missed_check(env):
    env.twitch.live = {"1": stream()}
    env.run(T0)
    env.twitch.live = {}
    env.run(T0 + CHECK)
    env.run(T0 + 2 * CHECK)
    assert env.webhook.edits[-1][1]["embeds"][0]["timestamp"] == "2026-10-01T12:05:00+00:00"


def test_short_dropout_keeps_the_message(env):
    env.twitch.live = {"1": stream()}
    env.run(T0)
    env.twitch.live = {}
    env.run(T0 + CHECK)
    env.twitch.live = {"1": stream()}
    env.run(T0 + 2 * CHECK)
    assert len(env.webhook.sent) == 1 and env.webhook.edits == []
    assert env.state["streams"]["1"]["misses"] == 0 and env.state["streams"]["1"]["missing_since"] is None


def test_quick_restart_keeps_the_message(env):
    env.twitch.live = {"1": stream()}
    env.run(T0)
    env.twitch.live = {}
    env.run(T0 + CHECK)
    env.twitch.live = {"1": stream(stream_id="s2", started_at="2026-10-01T12:07:00Z")}
    env.run(T0 + 2 * CHECK)
    assert len(env.webhook.sent) == 1
    assert env.state["streams"]["1"]["stream_id"] == "s2"
    assert env.state["streams"]["1"]["started_at"] == "2026-10-01T11:55:00Z"  # message keeps the first start


def test_restart_between_two_checks_without_vod_keeps_the_message(env):
    env.twitch.live = {"1": stream()}
    env.run(T0)
    env.twitch.live = {"1": stream(stream_id="s2")}
    env.run(T0 + CHECK)
    assert len(env.webhook.sent) == 1 and env.state["streams"]["1"]["stream_id"] == "s2"


def test_new_stream_long_after_the_last_one_gets_a_new_message(env):
    env.twitch.live = {"1": stream()}
    env.run(T0)
    env.twitch.live = {"1": stream(stream_id="s2", started_at="2026-10-01T18:00:00Z")}
    env.twitch.vod_ends = {"s1": datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)}
    assert env.run(T0 + timedelta(hours=6)) == ["CarolinaWWM's stream ended", "CarolinaWWM went live"]
    assert env.webhook.edits[-1][1]["embeds"][0]["timestamp"] == "2026-10-01T14:00:00+00:00"
    assert len(env.webhook.sent) == 2 and env.state["streams"]["1"]["message_id"] == "102"


def test_removed_streamer_still_gets_its_message_ended(env):
    env.twitch.live = {"1": stream()}
    env.run(T0)
    nobody = Config(streamers=())
    env.twitch.live = {"1": stream(stream_id="s2", started_at="2026-10-01T20:00:00Z")}
    env.twitch.vod_ends = {"s1": datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)}
    assert env.run(T0 + timedelta(hours=8), nobody) == ["CarolinaWWM's stream ended"]
    assert len(env.webhook.sent) == 1 and env.state["streams"] == {}


def test_deleted_live_message_is_left_alone(env):
    env.twitch.live = {"1": stream()}
    env.run(T0)
    env.webhook.fail_edit = discord_error(404, notify.UNKNOWN_MESSAGE)
    env.twitch.live = {"1": stream(title="new title")}
    env.run(T0 + CHECK)
    assert env.state["streams"]["1"]["message_id"] is None
    env.webhook.fail_edit = None
    env.twitch.live = {}
    env.run(T0 + 2 * CHECK)
    env.run(T0 + 3 * CHECK)
    assert env.webhook.edits == [] and env.state["streams"] == {} and len(env.webhook.sent) == 1


def test_deleted_webhook_is_a_config_error(env):
    env.twitch.live = {"1": stream()}
    env.webhook.fail_send = discord_error(404, notify.UNKNOWN_WEBHOOK)
    with pytest.raises(ConfigError, match="webhook"):
        env.run(T0)


def test_discord_outage_is_retried_on_the_next_run(env, capsys):
    env.twitch.live = {"1": stream()}
    env.webhook.fail_send = HttpError(503, "upstream error", "https://discord.com/api/webhooks/1/***")
    assert env.run(T0) == []
    assert "::warning::CarolinaWWM: HTTP 503" in capsys.readouterr().out
    assert env.state["streams"] == {}
    assert env.run(T0 + CHECK) == ["CarolinaWWM went live"]


def test_watch_button_is_dropped_if_discord_refuses_it(env, capsys):
    env.twitch.live = {"1": stream()}
    env.webhook.fail_send = HttpError(400, '{"message": "Invalid Form Body"}', "x")
    assert env.run(T0) == ["CarolinaWWM went live"]
    assert "components" not in env.webhook.sent[0]
    assert "Watch Stream button" in capsys.readouterr().out


def test_pings(env):
    env.twitch.live = {"1": stream()}
    env.run(T0, Config(streamers=("carolinawwm",), ping="555"))
    assert env.webhook.sent[0]["content"] == "<@&555> **CarolinaWWM** is live on Twitch!"
    assert env.webhook.sent[0]["allowed_mentions"] == {"parse": [], "roles": ["555"]}
    payload = notify.live_message(Config(streamers=(), ping="everyone"), stream(), "", T0)
    assert payload["content"] == "@everyone **CarolinaWWM** is live on Twitch!"
    assert payload["allowed_mentions"] == {"parse": ["everyone"]}


def test_unknown_streamer_is_a_warning_not_a_crash(env, capsys):
    assert env.run(T0, Config(streamers=("nobody_here",))) == []
    assert "no Twitch user called 'nobody_here'" in capsys.readouterr().out


# --- small pieces -------------------------------------------------------------------------------

@pytest.mark.parametrize("text, login", [
    ("CarolinaWWM", "carolinawwm"), ("  @IIGreyl ", "iigreyl"),
    ("https://www.twitch.tv/Some_Streamer", "some_streamer"), ("not a name", None), ("", None),
])
def test_parse_login(text, login):
    assert notify.parse_login(text) == login


def test_parse_duration():
    assert notify.parse_duration("3h8m33s") == timedelta(hours=3, minutes=8, seconds=33)
    assert notify.parse_duration("45s") == timedelta(seconds=45)
    with pytest.raises(ValueError):
        notify.parse_duration("")


def test_markdown_is_escaped_where_it_renders():
    payload = notify.ended_message("Cool_Guy_", "cool_guy_", T0)
    assert payload["content"] == r"**Cool\_Guy\_** was live. The stream has ended."
    assert payload["embeds"][0]["author"]["name"] == "Cool_Guy_ was live on Twitch"
    assert payload["embeds"][0]["description"] == r"Cool\_Guy\_'s stream has ended."


def test_webhook_url():
    hook = notify.Webhook("https://discord.com/api/webhooks/123/abc-DEF_9")
    assert hook.base == "https://discord.com/api/v10/webhooks/123/abc-DEF_9"
    notify.Webhook("https://canary.discordapp.com/api/v9/webhooks/123/abc/")
    with pytest.raises(ConfigError):
        notify.Webhook("https://example.com/api/webhooks/123/abc")


def test_webhook_token_never_appears_in_errors():
    assert notify._redact("https://discord.com/api/v10/webhooks/1/s3cret/messages/2?with_components=true") == (
        "https://discord.com/api/v10/webhooks/1/***/messages/2?with_components=true")


def test_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('streamers = ["CarolinaWWM", "twitch.tv/carolinawwm", "@IIGreyl"]\nping = "<@&123>"\n',
                    encoding="utf-8")
    config = notify.load_config(path)
    assert config.streamers == ("carolinawwm", "iigreyl")
    assert (config.ping, config.name, config.watch_button) == ("123", "", True)
    for bad in ('streamers = ["no spaces allowed"]', 'ping = "mods"', "streamers = ["):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ConfigError):
            notify.load_config(path)


def test_state_is_only_written_when_it_changes(tmp_path):
    path = tmp_path / "state.json"
    state = notify.load_state(path)
    assert state == {"streams": {}}
    assert notify.save_state(state, path) is True
    assert notify.save_state(notify.load_state(path), path) is False


# --- the whole run against fake Twitch and Discord servers --------------------------------------

@pytest.fixture
def apis():
    calls = []
    live = {"1": stream()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload):
            raw = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()

        def do_POST(self):
            body = self.body()
            calls.append(("POST", self.path, body))
            if self.path == "/oauth2/token":
                form = parse_qs(body)
                if form.get("client_secret") != ["secret"]:
                    return self.reply(403, {"status": 403, "message": "invalid client secret"})
                assert form["grant_type"] == ["client_credentials"]
                return self.reply(200, {"access_token": "tok", "expires_in": 3600, "token_type": "bearer"})
            if self.path == "/oauth2/revoke":
                return self.reply(200, None)
            assert self.path == "/api/webhooks/1/hooktoken?wait=true&with_components=true"
            if sum(1 for call in calls if call[1].startswith("/api/webhooks")) == 1:
                return self.reply(429, {"message": "You are being rate limited.", "retry_after": 0.05})
            return self.reply(200, {"id": "999"})

        def do_GET(self):
            calls.append(("GET", self.path, ""))
            assert self.headers["Authorization"] == "Bearer tok" and self.headers["Client-Id"] == "cid"
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/helix/users":
                return self.reply(200, {"data": [CAROL] if "carolinawwm" in query.get("login", []) else []})
            if url.path == "/helix/streams":
                return self.reply(200, {"data": [live[i] for i in query.get("user_id", []) if i in live]})
            return self.reply(404, {"message": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_address[1]}", calls=calls, live=live)
    server.shutdown()


@pytest.fixture
def repo(tmp_path, monkeypatch, apis):
    (tmp_path / "config.toml").write_text('streamers = ["carolinawwm"]\n', encoding="utf-8")
    monkeypatch.setattr(notify, "ROOT", tmp_path)
    monkeypatch.setattr(notify, "TWITCH_TOKEN_URL", f"{apis.base}/oauth2/token")
    monkeypatch.setattr(notify, "TWITCH_REVOKE_URL", f"{apis.base}/oauth2/revoke")
    monkeypatch.setattr(notify, "TWITCH_API", f"{apis.base}/helix")
    monkeypatch.setattr(notify, "DISCORD_API", f"{apis.base}/api")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/hooktoken")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return tmp_path


def test_main_posts_and_saves_state(repo, apis):
    assert notify.main() == 0
    state = json.loads((repo / "state.json").read_text(encoding="utf-8"))
    assert state["streams"]["1"]["message_id"] == "999"
    assert "keepalive" in state
    assert (repo / "output").read_text(encoding="utf-8") == "changed=true\nsummary=CarolinaWWM went live\n"
    assert "| carolinawwm | 🔴 live |" in (repo / "summary.md").read_text(encoding="utf-8")
    webhook_calls = [call for call in apis.calls if call[1].startswith("/api/webhooks")]
    assert len(webhook_calls) == 2  # the first one got a 429 and was retried
    assert apis.calls[-1][1] == "/oauth2/revoke"  # the run's token is revoked at the end

    # next run: still live, nothing to do, nothing to commit
    assert notify.main() == 0
    assert (repo / "output").read_text(encoding="utf-8").endswith("changed=false\nsummary=Update state\n")


def test_main_reports_wrong_twitch_secret(repo, monkeypatch, capsys):
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "wrong")
    assert notify.main() == 1
    assert "::error::Twitch rejected" in capsys.readouterr().out


def test_main_reports_missing_secrets(repo, monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    assert notify.main() == 1
    assert "::error::DISCORD_WEBHOOK_URL is not set" in capsys.readouterr().out
