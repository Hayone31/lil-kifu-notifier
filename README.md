# Lil Kifu

Twitch go-live notifications for a Discord channel, with no server to run. GitHub Actions checks Twitch
every 5 minutes and posts through a Discord webhook. When the stream ends, the same message is edited:

| While live | After the stream |
|---|---|
| **CarolinaWWM** is live on Twitch! | **CarolinaWWM** was live. The stream has ended. |
| *CarolinaWWM is live on Twitch*<br>**[Stream title](https://twitch.tv)**<br>Playing **Game**<br>profile picture, live preview, `Watch Stream` button | *CarolinaWWM was live on Twitch*<br>**[Stream ended](https://twitch.tv)**<br>CarolinaWWM's stream has ended.<br>time the stream ended |

## Add or remove streamers

Edit [`config.toml`](config.toml) on GitHub (pencil icon), change the `streamers` list and commit.
The next check uses the new list. The same file sets who gets pinged, the name and avatar of the
messages, and the Watch Stream button.

## Setup

1. **Discord webhook:** open the channel's settings, go to **Integrations → Webhooks → New Webhook**,
   name it *Lil Kifu*, upload the avatar and click **Copy Webhook URL**.
2. **Twitch app:** open https://dev.twitch.tv/console/apps and click **Register Your Application**.
   Use OAuth redirect `http://localhost`, category *Application Integration*, client type *Confidential*.
   Copy the **Client ID**, then click **New Secret** and copy the secret.
3. **Repository secrets:** go to **Settings → Secrets and variables → Actions → New repository secret**
   and add `DISCORD_WEBHOOK_URL`, `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.
4. **First run:** go to **Actions → Twitch notifications → Run workflow**. After that it runs on its own every 5 minutes.

## How it works

- Each run takes a few seconds. It gets a Twitch app token, asks which streamers are live and updates
  Discord. At the end it revokes the token.
- `state.json` remembers which live messages are open. The workflow commits it whenever something
  changes, so the commit history doubles as a stream log. Don't edit it by hand.
- A stream counts as ended once it has been missing on two checks in a row, which protects against
  Twitch hiccups and stream crashes. A stream that comes back within that window keeps its message.
- When the streamer keeps VODs, the end time shown in Discord comes from the VOD. Otherwise it is the
  time of the first check that saw the stream offline.
- If Twitch or Discord is down, the run logs a warning and the next run tries again. Wrong secrets fail
  the run, and GitHub emails you about failed runs.

## Limits

- GitHub runs scheduled workflows every 5 minutes at best. When GitHub is busy, runs can start 10-20
  minutes late, and the notifications arrive late with them.
- GitHub pauses scheduled workflows in repositories with no activity for 60 days. The workflow commits
  `state.json` at least every 25 days to prevent that. If the schedule does get paused, re-enable it in
  the Actions tab.
- The repository is public because Actions minutes are only free without limit for public
  repositories. Secrets stay private. `config.toml` and `state.json` are visible to anyone.

## Local test run

Put the three secrets in a `.env` file next to `notify.py` (one `NAME=value` per line) and run
`python notify.py`. Run the tests with `python -m pytest`; they need only pytest.
