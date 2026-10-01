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
    secret_name = "DISCORD_WEBHOOK_URL"

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


def test_webhook_url_paste_mistakes_are_forgiven():
    for raw in ('"https://discord.com/api/webhooks/123/abc"', " https://discord.com/api/webhooks/123/abc \n",
                "DISCORD_TIKTOK_WEBHOOK_URL=https://discord.com/api/webhooks/123/abc",
                "https://discord.com/api/webhooks/123/abc?thread_id=5"):
        assert notify.Webhook(raw).base == "https://discord.com/api/v10/webhooks/123/abc"


@pytest.mark.parametrize("raw, hint", [
    ("", "empty"),
    ("https://discord.com/api/webhooks/123/a b", "spaces"),
    ("discord.com/api/webhooks/123/abc", "https://"),
    ("https://discord.com/channels/1/2", "isn't a webhook link"),
    ("https://discord.com/api/webhooks/abc", "<number>/<token>"),
])
def test_bad_webhook_urls_are_explained_without_echoing_them(raw, hint):
    with pytest.raises(ConfigError) as info:
        notify.Webhook(raw, "DISCORD_TIKTOK_WEBHOOK_URL")
    message = str(info.value)
    assert hint in message and message.startswith("DISCORD_TIKTOK_WEBHOOK_URL is not")
    assert not raw or raw.strip() not in message


def test_webhook_token_never_appears_in_errors():
    assert notify._redact("https://discord.com/api/v10/webhooks/1/s3cret/messages/2?with_components=true") == (
        "https://discord.com/api/v10/webhooks/1/***/messages/2?with_components=true")


def test_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[twitch]\nstreamers = ["CarolinaWWM", "twitch.tv/carolinawwm", "@IIGreyl"]\nping = "<@&123>"\n'
        '[tiktok]\naccounts = ["BeagleMommy", "https://www.tiktok.com/@beaglemommy", "@some.one_2"]\n'
        'name = "Lil Nao"\napi = "https://panel.example/"\n', encoding="utf-8")
    config = notify.load_config(path)
    assert config.streamers == ("carolinawwm", "iigreyl")
    assert (config.ping, config.name, config.watch_button) == ("123", "", True)
    assert config.tiktok.accounts == ("BeagleMommy", "some.one_2")
    assert (config.tiktok.ping, config.tiktok.name, config.tiktok.api) == ("", "Lil Nao", "https://panel.example")
    for bad in ('streamers = ["no spaces allowed"]', '[twitch]\nping = "mods"', "streamers = [",
                '[tiktok]\naccounts = ["no spaces"]', '[tiktok]\nping = "x"'):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ConfigError):
            notify.load_config(path)


def test_config_without_sections_still_works(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('streamers = ["carolinawwm"]\nname = "Lil Kifu"\n', encoding="utf-8")
    config = notify.load_config(path)
    assert (config.streamers, config.name, config.tiktok.accounts) == (("carolinawwm",), "Lil Kifu", ())


# --- TikTok -------------------------------------------------------------------------------------

TIKTOK_CONFIG = notify.TikTokConfig(accounts=("BeagleMommy",), name="Lil Nao")


def no_urlebird(handle):
    raise notify.TikTokError("urlebird is off in this test")

ON_TIME = datetime(2026, 10, 1, 12, 2, tzinfo=timezone.utc)     # minute 2: a TikTok check is due
OFF_TIME = datetime(2026, 10, 1, 12, 7, tzinfo=timezone.utc)    # minute 7: not due


class FakeTikTok:
    def __init__(self, count=104):
        self.count = count
        self.fail = None
        self.calls = 0

    def __call__(self, handle):
        self.calls += 1
        assert handle == "beaglemommy"
        if self.fail:
            raise self.fail
        return {"video_count": self.count, "avatar": "https://cdn/avatar.jpg"}


def test_tiktok_lifecycle():
    state, hook, tiktok = {"streams": {}}, FakeWebhook(), FakeTikTok()
    run = lambda at, **kw: notify.check_tiktok(TIKTOK_CONFIG, state, hook, at, list_videos=no_urlebird, fetch=tiktok, **kw)

    assert run(OFF_TIME) == []  # a new account is looked at right away, but only remembered
    assert state["tiktok"] == {"beaglemommy": {"video_count": 104}} and hook.sent == []

    tiktok.count = 105
    assert run(OFF_TIME) == [] and tiktok.calls == 1  # not due yet
    assert run(ON_TIME) == ["BeagleMommy uploaded a new TikTok"]
    assert hook.sent == [{
        "embeds": [{"title": "BeagleMommy uploaded a new TikTok!", "color": 0xDB4263,
                    "thumbnail": {"url": "https://cdn/avatar.jpg"}}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "View on TikTok", "url": "https://www.tiktok.com/@beaglemommy"}]}],
        "allowed_mentions": {"parse": []},
        "username": "Lil Nao",
    }]

    tiktok.count = 104  # a video was deleted: follow the count down quietly
    assert run(ON_TIME) == [] and state["tiktok"]["beaglemommy"]["video_count"] == 104
    tiktok.count = 107
    assert run(OFF_TIME, force=True) == ["BeagleMommy uploaded a new TikTok"]
    assert hook.sent[-1]["embeds"][0]["title"] == "BeagleMommy uploaded 3 new TikToks!"


def test_tiktok_failures_are_retried():
    state, hook, tiktok = {"streams": {}, "tiktok": {"beaglemommy": {"video_count": 104}}}, FakeWebhook(), FakeTikTok(105)
    tiktok.fail = notify.TikTokError("blocked")
    assert notify.check_tiktok(TIKTOK_CONFIG, state, hook, ON_TIME, list_videos=no_urlebird, fetch=tiktok) == []
    hook.fail_send = HttpError(503, "down", "x")
    tiktok.fail = None
    assert notify.check_tiktok(TIKTOK_CONFIG, state, hook, ON_TIME, list_videos=no_urlebird, fetch=tiktok) == []
    assert state["tiktok"]["beaglemommy"]["video_count"] == 104  # not counted as announced
    assert notify.check_tiktok(TIKTOK_CONFIG, state, hook, ON_TIME, list_videos=no_urlebird, fetch=tiktok) == ["BeagleMommy uploaded a new TikTok"]


def test_tiktok_role_ping_and_removed_accounts():
    payload = notify.tiktok_message(notify.TikTokConfig(ping="555"), "BeagleMommy", "", 1)
    assert payload["content"] == "<@&555>" and payload["allowed_mentions"] == {"parse": [], "roles": ["555"]}
    assert "thumbnail" not in payload["embeds"][0] and "username" not in payload
    state = {"streams": {}, "tiktok": {"gone": {"video_count": 1}}}
    notify.check_tiktok(notify.TikTokConfig(), state, FakeWebhook(), ON_TIME, list_videos=no_urlebird, fetch=FakeTikTok())
    assert state["tiktok"] == {}


CONNECTED = notify.TikTokConfig(accounts=("BeagleMommy",), name="Lil Nao", api="https://panel.example")


def video(number, caption="One More Level! #fy #soulslike", created=None):
    return {"id": f"76{number:017d}", "caption": caption, "cover": f"https://cdn/cover{number}.jpg",
            "url": f"https://www.tiktok.com/@beaglemommy/video/76{number:017d}", "created": created or 1790000000 + number}


class FakeVideos:
    def __init__(self, videos=None):
        self.videos = videos if videos is not None else [video(1)]
        self.fail = None

    def __call__(self, api, handle):
        assert (api, handle) == ("https://panel.example", "beaglemommy")
        if self.fail:
            raise self.fail
        return list(reversed(sorted(self.videos, key=lambda v: v["created"])))  # newest first, like TikTok


def test_tiktok_official_api_posts_caption_cover_and_link():
    state, hook, api = {"streams": {}}, FakeWebhook(), FakeVideos()
    run = lambda: notify.check_tiktok(CONNECTED, state, hook, ON_TIME, list_videos=no_urlebird, fetch=FakeTikTok(), fetch_videos=api)

    assert run() == [] and hook.sent == []  # first look only remembers what's there
    api.videos.append(video(2, caption="Boss_fight *clip*"))
    assert run() == ["BeagleMommy uploaded a new TikTok"]
    assert hook.sent == [{
        "embeds": [{"title": "BeagleMommy uploaded a new TikTok!", "color": 0xDB4263,
                    "description": r"Boss\_fight \*clip\*", "image": {"url": "https://cdn/cover2.jpg"}}],
        "components": [{"type": 1, "components": [{"type": 2, "style": 5, "label": "View on TikTok",
                                                   "url": "https://www.tiktok.com/@beaglemommy/video/7600000000000000002"}]}],
        "allowed_mentions": {"parse": []},
        "username": "Lil Nao",
    }]
    api.videos = [video(2)]  # a video was deleted: nothing to announce
    assert run() == []
    api.videos += [video(n) for n in range(3, 9)]  # a burst after an outage: only the newest three are posted
    assert run() == ["BeagleMommy uploaded a new TikTok"] * 3
    assert [p["embeds"][0]["image"]["url"] for p in hook.sent[-3:]] == [f"https://cdn/cover{n}.jpg" for n in (6, 7, 8)]
    assert run() == []


def test_tiktok_official_api_failed_post_is_retried():
    state, hook, api = {"streams": {}}, FakeWebhook(), FakeVideos()
    run = lambda: notify.check_tiktok(CONNECTED, state, hook, ON_TIME, list_videos=no_urlebird, fetch=FakeTikTok(), fetch_videos=api)
    run()
    api.videos.append(video(2))
    hook.fail_send = HttpError(503, "down", "x")
    assert run() == []
    assert run() == ["BeagleMommy uploaded a new TikTok"]


def test_tiktok_falls_back_to_counting_when_not_connected_or_api_is_down(capsys):
    state, hook, api, counts = {"streams": {}}, FakeWebhook(), FakeVideos(), FakeTikTok(104)
    run = lambda: notify.check_tiktok(CONNECTED, state, hook, ON_TIME, list_videos=no_urlebird, fetch=counts, fetch_videos=api)
    api.fail = HttpError(502, '{"error": "approval expired"}', "x")
    assert run() == [] and state["tiktok"]["beaglemommy"] == {"video_count": 104}
    assert "the official API isn't answering" in capsys.readouterr().out
    counts.count = 105
    assert run() == ["BeagleMommy uploaded a new TikTok"]
    assert hook.sent[-1]["embeds"][0] == {"title": "BeagleMommy uploaded a new TikTok!", "color": 0xDB4263,
                                          "thumbnail": {"url": "https://cdn/avatar.jpg"}}
    not_connected = lambda api_url, handle: None
    counts.count = 106
    notify.check_tiktok(CONNECTED, state, hook, ON_TIME, list_videos=no_urlebird, fetch=counts, fetch_videos=not_connected)
    assert hook.sent[-1]["embeds"][0]["title"] == "BeagleMommy uploaded a new TikTok!" and len(hook.sent) == 2


def test_switching_between_api_and_counting_never_reannounces():
    state, hook = {"streams": {}, "tiktok": {"beaglemommy": {"video_count": 104}}}, FakeWebhook()
    api, counts = FakeVideos([video(1), video(2)]), FakeTikTok(110)
    notify.check_tiktok(CONNECTED, state, hook, ON_TIME, list_videos=no_urlebird, fetch=counts, fetch_videos=api)
    assert state["tiktok"]["beaglemommy"] == {"seen": ["7600000000000000002", "7600000000000000001"]}
    api.fail = HttpError(502, "{}", "x")
    notify.check_tiktok(CONNECTED, state, hook, ON_TIME, list_videos=no_urlebird, fetch=counts, fetch_videos=api)
    assert state["tiktok"]["beaglemommy"] == {"video_count": 110} and hook.sent == []


def counter(kind, count):
    return {"@type": "InteractionCounter", "interactionType": {"@type": f"http://schema.org/{kind}"},
            "userInteractionCount": count}


URLEBIRD_JSON_LD = json.dumps({"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
    {"position": "1", "@type": "VideoObject", "url": "https://urlebird.com/video/more-than-a-guild-7686796925315697952/",
     "interactionStatistic": [counter("WatchAction", 561), counter("LikeAction", 28), counter("CommentAction", 7),
                              counter("ShareAction", 18)]}]})
URLEBIRD_PAGE = f"""<html><head><title>Renaissance Guild WWM (@renaissance.guild) - Urlebird</title>
<script type="application/ld+json">{URLEBIRD_JSON_LD}</script></head><body>
<a href="https://urlebird.com/video/more-than-a-guild-7686796925315697952/">More than a guild</a>
<a href='https://urlebird.com/video/first-post-7600000000000000001/'><img></a>
<a href="https://urlebird.com/user/renaissance.guild/">profile</a></body></html>"""


def test_urlebird_page_parsing(monkeypatch):
    page = URLEBIRD_PAGE
    monkeypatch.setattr(notify, "http", lambda *args, **kwargs: page)
    videos = notify.urlebird_videos("renaissance.guild")
    assert [v["id"] for v in videos] == ["7686796925315697952", "7600000000000000001"]  # newest first
    assert datetime.fromtimestamp(videos[0]["created"], timezone.utc).date().isoformat() == "2026-09-18"
    assert videos[0]["stats"] == {"views": 561, "likes": 28, "comments": 7, "shares": 18}
    assert "stats" not in videos[1]
    page = "<html><title>Just a moment...</title></html>"  # e.g. a bot check instead of the profile
    with pytest.raises(notify.TikTokError, match="unexpected page"):
        notify.urlebird_videos("renaissance.guild")


def test_tiktok_video_details_checks_the_author(monkeypatch):
    seen_urls = []

    def fake_http(method, url, **kwargs):
        seen_urls.append(url)
        return {"author_unique_id": author, "title": "More than a guild #wwm", "thumbnail_url": "https://cdn/c.jpg"}
    monkeypatch.setattr(notify, "http", fake_http)
    author = "renaissance.guild"
    details = notify.tiktok_video_details("renaissance.guild", {"id": "7686796925315697952", "created": 1})
    assert details == {"id": "7686796925315697952", "created": 1, "caption": "More than a guild #wwm",
                       "cover": "https://cdn/c.jpg", "url": "https://www.tiktok.com/@renaissance.guild/video/7686796925315697952"}
    assert seen_urls[0] == ("https://www.tiktok.com/oembed?url=https%3A%2F%2Fwww.tiktok.com%2F%40renaissance.guild"
                            "%2Fvideo%2F7686796925315697952")
    author = "someone.else"
    assert notify.tiktok_video_details("renaissance.guild", {"id": "1", "created": 1}) is None


class FakeListing:
    def __init__(self, *numbers):
        self.numbers = list(numbers)
        self.fail = None

    def __call__(self, handle):
        if self.fail:
            raise self.fail
        return [{"id": video(n)["id"], "created": video(n)["created"]} for n in sorted(self.numbers, reverse=True)]


def test_urlebird_and_oembed_post_caption_cover_and_link():
    state, hook, listing = {"streams": {}, "tiktok": {"beaglemommy": {"video_count": 104}}}, FakeWebhook(), FakeListing(1)
    failures = {}

    def details(handle, item):
        if item["id"] in failures:
            raise failures[item["id"]]
        if item["id"].endswith("3"):
            return None  # another account's video showed up on the page
        number = int(item["id"][-2:])
        return {**item, "caption": f"clip {number}", "cover": f"https://cdn/cover{number}.jpg",
                "url": f"https://www.tiktok.com/@beaglemommy/video/{item['id']}"}

    run = lambda: notify.check_tiktok(TIKTOK_CONFIG, state, hook, ON_TIME, fetch=FakeTikTok(), list_videos=listing,
                                      details=details)
    assert run() == [] and state["tiktok"]["beaglemommy"] == {"seen": [video(1)["id"]]}  # switch from counting: no repeat
    listing.numbers = [1, 2, 3]
    assert run() == ["BeagleMommy uploaded a new TikTok"]
    [post] = hook.sent
    assert post["embeds"][0] == {"title": "BeagleMommy uploaded a new TikTok!", "color": 0xDB4263,
                                 "description": "clip 2", "image": {"url": "https://cdn/cover2.jpg"}}
    assert post["components"][0]["components"][0]["url"] == f"https://www.tiktok.com/@beaglemommy/video/{video(2)['id']}"
    assert video(3)["id"] in state["tiktok"]["beaglemommy"]["seen"]  # skipped, not retried

    listing.numbers = [1, 2, 3, 4, 5]
    failures[video(4)["id"]] = HttpError(404, "{}", "x")  # gone again: just counts as seen
    failures[video(5)["id"]] = HttpError(503, "{}", "x")  # TikTok hiccup: tried again next time
    assert run() == []
    del failures[video(5)["id"]]
    assert run() == ["BeagleMommy uploaded a new TikTok"] and hook.sent[-1]["embeds"][0]["description"] == "clip 5"
    assert run() == []


def test_urlebird_down_falls_back_to_counting(capsys):
    state, hook, listing, counts = {"streams": {}}, FakeWebhook(), FakeListing(1), FakeTikTok(104)
    listing.fail = HttpError(403, "Just a moment...", "x")
    assert notify.check_tiktok(TIKTOK_CONFIG, state, hook, ON_TIME, fetch=counts, list_videos=listing) == []
    assert state["tiktok"]["beaglemommy"] == {"video_count": 104}
    assert "urlebird isn't answering" in capsys.readouterr().out


def test_tiktok_footer_shows_likes_and_comments():
    embed = notify.tiktok_video_embed("X", {"caption": "c", "stats": {"likes": 28, "comments": 7, "views": 561}})
    assert embed["footer"] == {"text": "❤️ 28 · 💬 7"}
    assert "footer" not in notify.tiktok_video_embed("X", {"caption": "c"})
    assert [notify.compact(n) for n in (0, 999, 1000, 1234, 15300, 1_500_000, 2_000_000_000)] == [
        "0", "999", "1K", "1.2K", "15.3K", "1.5M", "2B"]


def test_counts_are_refreshed_after_1_3_6_and_24_hours():
    state, hook = {"streams": {}, "tiktok": {"beaglemommy": {"seen": [video(1)["id"]]}}}, FakeWebhook()
    stats = {"likes": 0, "comments": 0}
    listing = lambda handle: [{"id": video(n)["id"], "created": video(n)["created"], "stats": dict(stats)} for n in (2, 1)]
    covers = iter(range(100))

    def details(handle, item):
        return {**item, "caption": "clip", "cover": f"https://cdn/fresh{next(covers)}.jpg",
                "url": f"https://www.tiktok.com/@beaglemommy/video/{item['id']}"}

    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    run = lambda at: notify.check_tiktok(TIKTOK_CONFIG, state, hook, at, force=True, fetch=FakeTikTok(),
                                         list_videos=listing, details=details)
    assert run(t0) == ["BeagleMommy uploaded a new TikTok"]
    assert hook.sent[0]["embeds"][0]["footer"] == {"text": "❤️ 0 · 💬 0"}
    [post] = state["tiktok"]["beaglemommy"]["posted"]
    assert (post["message_id"], post["refreshes"]) == ("101", 0)

    stats.update(likes=12, comments=3)
    run(t0 + timedelta(minutes=30))
    assert hook.edits == []  # too early
    run(t0 + timedelta(hours=1))
    [(message_id, payload)] = hook.edits
    assert message_id == "101" and payload["embeds"][0]["footer"] == {"text": "❤️ 12 · 💬 3"}
    assert payload["embeds"][0]["image"]["url"] == "https://cdn/fresh1.jpg"  # a fresh cover link
    run(t0 + timedelta(hours=3))  # nothing changed: no edit, but the 3-hour update is done
    assert len(hook.edits) == 1 and state["tiktok"]["beaglemommy"]["posted"][0]["refreshes"] == 2
    stats.update(likes=1500)
    run(t0 + timedelta(hours=6))
    assert hook.edits[-1][1]["embeds"][0]["footer"] == {"text": "❤️ 1.5K · 💬 3"}
    hook.fail_edit = HttpError(404, json.dumps({"code": notify.UNKNOWN_MESSAGE}), "x")  # someone deleted the post
    stats.update(likes=2000)
    run(t0 + timedelta(hours=24))
    assert "posted" not in state["tiktok"]["beaglemommy"]
    assert len(hook.sent) == 1


def test_tiktok_videos_asks_the_panel(monkeypatch):
    def fake_http(method, url, **kwargs):
        if "nobody" in url:
            raise HttpError(404, '{"connected": false}', url)
        assert url == "https://panel.example/tiktok/videos?account=beaglemommy"
        return {"connected": True, "videos": [video(1)]}
    monkeypatch.setattr(notify, "http", fake_http)
    assert notify.tiktok_videos("https://panel.example", "beaglemommy") == [video(1)]
    assert notify.tiktok_videos("https://panel.example", "nobody") is None


def test_tiktok_profile_page_parsing(monkeypatch):
    data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 0, "userInfo": {
        "user": {"uniqueId": "beaglemommy", "avatarLarger": "https://cdn/big.jpg"},
        "stats": {"videoCount": 104}, "itemList": []}}}}
    page = f'<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(data)}</script></html>'
    monkeypatch.setattr(notify, "http", lambda *a, **kw: page)
    assert notify.tiktok_profile("beaglemommy") == {"video_count": 104, "avatar": "https://cdn/big.jpg"}
    data["__DEFAULT_SCOPE__"]["webapp.user-detail"]["statusCode"] = 10221
    page = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{json.dumps(data)}</script>'
    with pytest.raises(notify.TikTokError, match="10221"):
        notify.tiktok_profile("beaglemommy")
    page = "<html>captcha</html>"
    with pytest.raises(notify.TikTokError, match="no data"):
        notify.tiktok_profile("beaglemommy")


def test_parse_tiktok():
    assert notify.parse_tiktok("@BeagleMommy") == "BeagleMommy"
    assert notify.parse_tiktok("https://www.tiktok.com/@some.one_2?lang=en") == "some.one_2"
    assert notify.parse_tiktok("two words") is None


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
    tiktok = SimpleNamespace(count=104, videos=None, urlebird=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload, content_type="application/json"):
            if payload is None:
                raw = b""
            else:
                raw = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
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
            if self.path == "/api/webhooks/2/tiktoktoken?wait=true&with_components=true":
                return self.reply(200, {"id": "777"})
            assert self.path == "/api/webhooks/1/hooktoken?wait=true&with_components=true"
            if sum(1 for call in calls if call[1].startswith("/api/webhooks/1/")) == 1:
                return self.reply(429, {"message": "You are being rate limited.", "retry_after": 0.05})
            return self.reply(200, {"id": "999"})

        def do_GET(self):
            calls.append(("GET", self.path, ""))
            if self.path == "/user/beaglemommy/":
                if tiktok.urlebird is None:
                    return self.reply(404, None)
                links = "".join(f'<a href="https://urlebird.com/video/clip-{i}/">x</a>' for i in tiktok.urlebird)
                ld = json.dumps({"@type": "ItemList", "itemListElement": [
                    {"@type": "VideoObject", "url": f"https://urlebird.com/video/clip-{i}/",
                     "interactionStatistic": [counter("LikeAction", 5), counter("CommentAction", 2)]} for i in tiktok.urlebird]})
                page = f'<title>Brandy (@beaglemommy) - Urlebird</title><script type="application/ld+json">{ld}</script>{links}'
                return self.reply(200, page, "text/html")
            if self.path.startswith("/oembed?url="):
                video_id = parse_qs(urlparse(self.path).query)["url"][0].rsplit("/", 1)[1]
                return self.reply(200, {"author_unique_id": "beaglemommy", "title": f"caption of {video_id}",
                                        "thumbnail_url": f"https://cdn/{video_id}.jpg"})
            if self.path.startswith("/tiktok/videos?account="):
                if tiktok.videos is None:
                    return self.reply(404, {"connected": False})
                return self.reply(200, {"connected": True, "videos": tiktok.videos})
            if self.path == "/@beaglemommy":
                assert self.headers["User-Agent"].startswith("Mozilla/5.0")
                data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 0, "userInfo": {
                    "user": {"avatarLarger": "https://cdn/beagle.jpg"}, "stats": {"videoCount": tiktok.count}}}}}
                page = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(data)}</script>'
                return self.reply(200, page, "text/html; charset=utf-8")
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
    yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_address[1]}", calls=calls, live=live, tiktok=tiktok)
    server.shutdown()


@pytest.fixture
def repo(tmp_path, monkeypatch, apis):
    (tmp_path / "config.toml").write_text(
        '[twitch]\nstreamers = ["carolinawwm"]\n[tiktok]\naccounts = ["BeagleMommy"]\nname = "Lil Nao"\n',
        encoding="utf-8")
    monkeypatch.setattr(notify, "ROOT", tmp_path)
    monkeypatch.setattr(notify, "TWITCH_TOKEN_URL", f"{apis.base}/oauth2/token")
    monkeypatch.setattr(notify, "TWITCH_REVOKE_URL", f"{apis.base}/oauth2/revoke")
    monkeypatch.setattr(notify, "TWITCH_API", f"{apis.base}/helix")
    monkeypatch.setattr(notify, "TIKTOK_PROFILE_URL", f"{apis.base}/@{{}}")
    monkeypatch.setattr(notify, "URLEBIRD_URL", f"{apis.base}/user/{{}}/")
    monkeypatch.setattr(notify, "TIKTOK_OEMBED_URL", f"{apis.base}/oembed?url={{}}")
    monkeypatch.setattr(notify, "DISCORD_API", f"{apis.base}/api")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/hooktoken")
    monkeypatch.setenv("DISCORD_TIKTOK_WEBHOOK_URL", "https://discord.com/api/webhooks/2/tiktoktoken")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("RUN_SOURCE", raising=False)
    return tmp_path


def webhook_posts(apis, number):
    return [json.loads(call[2]) for call in apis.calls
            if call[0] == "POST" and call[1].startswith(f"/api/webhooks/{number}/")]


def test_main_posts_and_saves_state(repo, apis, monkeypatch):
    assert notify.main() == 0
    state = json.loads((repo / "state.json").read_text(encoding="utf-8"))
    assert state["streams"]["1"]["message_id"] == "999"
    assert state["tiktok"] == {"beaglemommy": {"video_count": 104}}  # first look: remembered, not announced
    assert "keepalive" in state
    assert (repo / "output").read_text(encoding="utf-8") == "changed=true\nsummary=CarolinaWWM went live\n"
    summary = (repo / "summary.md").read_text(encoding="utf-8")
    assert "| carolinawwm | 🔴 live |" in summary and "| TikTok @BeagleMommy | 104 videos |" in summary
    assert len(webhook_posts(apis, 1)) == 2  # the first one got a 429 and was retried
    assert webhook_posts(apis, 2) == []
    assert ("POST", "/oauth2/revoke") in [call[:2] for call in apis.calls]  # the run's token is revoked

    # next run: still live, nothing to do, nothing to commit
    assert notify.main() == 0
    assert (repo / "output").read_text(encoding="utf-8").endswith("changed=false\nsummary=Update state\n")

    # a new TikTok, checked right away because the run was started by hand
    apis.tiktok.count = 105
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    assert notify.main() == 0
    [post] = webhook_posts(apis, 2)
    assert post["username"] == "Lil Nao" and post["embeds"][0]["title"] == "BeagleMommy uploaded a new TikTok!"
    assert post["allowed_mentions"] == {"parse": []} and "content" not in post
    assert (repo / "output").read_text(encoding="utf-8").endswith(
        "changed=true\nsummary=BeagleMommy uploaded a new TikTok\n")


def test_main_finds_new_tiktoks_through_urlebird(repo, apis, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    apis.tiktok.urlebird = ["7686796925315697952"]
    assert notify.main() == 0
    assert json.loads((repo / "state.json").read_text(encoding="utf-8"))["tiktok"] == {
        "beaglemommy": {"seen": ["7686796925315697952"]}}
    apis.tiktok.urlebird = ["7686796925315697952", "7690000000000000001"]
    assert notify.main() == 0
    [post] = webhook_posts(apis, 2)
    assert post["embeds"][0] == {"title": "BeagleMommy uploaded a new TikTok!", "color": 0xDB4263,
                                 "description": "caption of 7690000000000000001",
                                 "image": {"url": "https://cdn/7690000000000000001.jpg"},
                                 "footer": {"text": "❤️ 5 · 💬 2"}}
    assert post["components"][0]["components"][0]["url"] == "https://www.tiktok.com/@beaglemommy/video/7690000000000000001"
    assert post["username"] == "Lil Nao" and post["allowed_mentions"] == {"parse": []}
    [followed] = json.loads((repo / "state.json").read_text(encoding="utf-8"))["tiktok"]["beaglemommy"]["posted"]
    assert (followed["id"], followed["message_id"], followed["stats"]) == ("7690000000000000001", "777", {"likes": 5, "comments": 2})


def test_main_with_tiktoks_official_api(repo, apis, monkeypatch):
    (repo / "config.toml").write_text(
        f'[twitch]\nstreamers = []\n[tiktok]\naccounts = ["BeagleMommy"]\nname = "Lil Nao"\napi = "{apis.base}"\n',
        encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    apis.tiktok.videos = [video(1)]
    assert notify.main() == 0
    assert json.loads((repo / "state.json").read_text(encoding="utf-8"))["tiktok"] == {
        "beaglemommy": {"seen": ["7600000000000000001"]}}
    apis.tiktok.videos = [video(2, caption="New clip"), video(1)]
    assert notify.main() == 0
    [post] = webhook_posts(apis, 2)
    assert post["embeds"][0]["description"] == "New clip"
    assert post["components"][0]["components"][0]["url"].endswith("/video/7600000000000000002")
    assert "| TikTok @BeagleMommy | watching new videos |" in (repo / "summary.md").read_text(encoding="utf-8")


def test_timer_runs_keep_tiktok_at_its_own_pace(repo, apis, monkeypatch):
    assert notify.main() == 0  # first look at the account
    apis.tiktok.count = 105
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUN_SOURCE", "timer")
    monkeypatch.setattr(notify, "tiktok_due", lambda now: False)  # whatever minute the test runs at
    assert notify.main() == 0
    assert webhook_posts(apis, 2) == []
    monkeypatch.setenv("RUN_SOURCE", "manual")
    assert notify.main() == 0
    assert len(webhook_posts(apis, 2)) == 1


def test_say_posts_through_the_chosen_webhook_without_pinging(repo, apis, monkeypatch, capsys):
    assert notify.say("hi love @everyone", "tiktok") == 0
    assert webhook_posts(apis, 2) == [
        {"content": "hi love @everyone", "allowed_mentions": {"parse": []}, "username": "Lil Nao"}]
    assert notify.say("hello", "twitch") == 0
    assert webhook_posts(apis, 1)[-1]["content"] == "hello"
    monkeypatch.delenv("DISCORD_TIKTOK_WEBHOOK_URL")
    assert notify.say("hi", "tiktok") == 1
    assert "::error::DISCORD_TIKTOK_WEBHOOK_URL is not set" in capsys.readouterr().out


def test_main_without_tiktok_webhook_still_does_twitch(repo, monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_TIKTOK_WEBHOOK_URL")
    assert notify.main() == 0
    assert "DISCORD_TIKTOK_WEBHOOK_URL is not set" in capsys.readouterr().out
    state = json.loads((repo / "state.json").read_text(encoding="utf-8"))
    assert state["streams"]["1"]["message_id"] == "999" and "tiktok" not in state


def test_main_reports_wrong_twitch_secret(repo, monkeypatch, capsys):
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "wrong")
    assert notify.main() == 1
    assert "::error::Twitch rejected" in capsys.readouterr().out


def test_main_reports_missing_secrets(repo, monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    assert notify.main() == 1
    assert "::error::DISCORD_WEBHOOK_URL is not set" in capsys.readouterr().out
