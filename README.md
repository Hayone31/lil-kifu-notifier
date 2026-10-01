# Lil Kifu & Lil Nao

Discord notifications for your community's streamers, with no server to run. GitHub Actions does the
checking and posts through Discord webhooks:

- **Lil Kifu** posts when a Twitch streamer goes live and edits the same message when the stream ends.
- **Lil Nao** posts when a TikTok account uploads something new.

| Lil Kifu, while live | Lil Kifu, after the stream | Lil Nao |
|---|---|---|
| **CarolinaWWM** is live on Twitch!<br>stream title, game, profile picture, live preview, `Watch Stream` button | **CarolinaWWM** was live. The stream has ended.<br>"Stream ended" with the time it ended | **BeagleMommy uploaded a new TikTok!**<br>profile picture, `View on TikTok` button |

## Admin commands in Discord

Server admins (members with **Manage Server**) can manage both lists without leaving Discord. Only
the admin who runs a command sees the answer.

| Command | What it does |
|---|---|
| `/twitch add <streamer>` | Lil Kifu announces this Twitch streamer. The name is checked on Twitch first. |
| `/twitch remove <streamer>` | Stop announcing them. The current list is offered as suggestions. |
| `/twitch list` | The Twitch list, showing who is live right now. |
| `/tiktok add <account>` | Lil Nao announces this TikTok account. The name is checked on TikTok first. |
| `/tiktok remove <account>` | Stop announcing it. |
| `/tiktok list` | The TikTok list with each account's video count. |

The commands are answered by a small Cloudflare Worker in [`panel/`](panel). It commits each
change to `config.toml`, and that commit starts a check right away. Nothing runs on anyone's PC.

The same Worker also starts the notification check every 5 minutes, because GitHub's own scheduler
often starts 5-minute workflows late or not at all. GitHub's schedule stays on as a backup, every
30 minutes.

The Worker's secrets are kept in Cloudflare, never in this repository: `DISCORD_PUBLIC_KEY`,
`ALLOWED_GUILD_IDS`, `GITHUB_TOKEN` (a fine-grained token for this repository only, with *Contents*
and *Actions* set to read and write), `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.

Setting it up:
1. In `panel/`, run `npx wrangler@4 deploy`.
2. Run `node --env-file=<file with DISCORD_TOKEN> register.mjs <worker URL>`. It registers the
   commands and points Discord at the Worker.
3. Add the server's ID to `ALLOWED_GUILD_IDS`. A server that isn't allowed yet sees its own ID when
   someone uses a command there.

## Add or remove accounts by hand

Edit [`config.toml`](config.toml) on GitHub (pencil icon) and commit. A check runs right away.

- `[twitch] streamers`: the Twitch usernames Lil Kifu watches.
- `[tiktok] accounts`: the TikTok usernames Lil Nao watches. Write each one the way it should
  appear in Discord, e.g. `"BeagleMommy"`.

Each section also sets who gets pinged (nobody by default), the name and avatar of the messages, and
Twitch's Watch Stream button.

## Setup

1. **Discord webhooks:** in the channel settings, go to **Integrations → Webhooks → New Webhook**.
   Create one named *Lil Kifu* and one named *Lil Nao*, give them their avatars and copy both URLs.
   They can post in the same channel or in different ones. You can move a webhook to another channel
   later without its URL changing.
2. **Twitch app:** at https://dev.twitch.tv/console/apps, click **Register Your Application**. Use
   OAuth redirect `http://localhost`, category *Application Integration* and client type
   *Confidential*. Copy the Client ID and click **New Secret**.
3. **Repository secrets:** go to **Settings → Secrets and variables → Actions → New repository secret**
   and add these four:
   - `DISCORD_WEBHOOK_URL`: the Lil Kifu webhook
   - `DISCORD_TIKTOK_WEBHOOK_URL`: the Lil Nao webhook
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`
4. **First run:** go to **Actions → Twitch notifications → Run workflow**. After that, it runs every
   5 minutes on its own.

## How it works

- **Twitch:** each run gets a Twitch app token, asks which streamers are live, updates Discord and
  revokes the token.
  - A stream counts as ended after it has been missing on two checks in a row. A crash and a quick
    reconnect keep the same message.
  - The end time shown comes from the stream's VOD when the streamer keeps VODs.
- **TikTok:** TikTok has no free API for this, and it hides the list of videos from data-centre IPs
  like GitHub's. A public profile still shows how many videos the account has, so Lil Nao reads that
  number about every 15 minutes and posts when it goes up. This has three consequences:
  - The message can't include the video's caption or cover. The button opens the profile, where the
    newest video is at the top.
  - If a video is deleted and a new one is posted between two checks, the count stays the same and
    that post is missed.
  - TikTok can change its pages at any time. If it does, TikTok checks log warnings until the code is
    adjusted. Twitch keeps working either way.
- `state.json` remembers open live messages and the last known TikTok counts. The workflow commits
  it when something changes, so the commit history doubles as a log. Don't edit it by hand.
- **Errors:**
  - When Twitch, TikTok or Discord is down, the run logs a warning and the next run tries again.
  - Wrong or missing secrets fail the run, and GitHub emails you about failed runs.
  - A missing `DISCORD_TIKTOK_WEBHOOK_URL` only skips TikTok.

## Limits

- Checks run every 5 minutes, so a notification can arrive up to about 5 minutes after the stream
  starts. If the panel Worker is down, only GitHub's 30-minute backup schedule is left.
- GitHub pauses scheduled workflows in repositories with no activity for 60 days. The workflow
  commits `state.json` at least every 25 days to prevent that. If it is ever paused, re-enable it in
  the Actions tab.
- The repository is public because GitHub Actions minutes are free and unlimited only in public
  repositories. Secrets stay private. `config.toml` and `state.json` are visible to anyone.

## Local test run

Put the secrets in a `.env` file next to `notify.py`, one `NAME=value` per line, and run
`python notify.py`. Run the tests with `python -m pytest`; they need only pytest.
