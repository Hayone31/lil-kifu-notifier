// One-time setup for the Lil Kifu panel:
//   - registers the /twitch and /tiktok commands for the Discord app,
//   - removes old commands the earlier PC bot registered in servers,
//   - points Discord at the Worker (Discord checks right away that the Worker answers).
//
//   node --env-file=PATH/TO/.env register.mjs [https://lil-kifu-panel.<you>.workers.dev]
//
// DISCORD_TOKEN (the bot token) must be in the environment. It is only ever sent to Discord.

const API = "https://discord.com/api/v10";
const token = process.env.DISCORD_TOKEN?.trim();
if (!token) {
  console.error("DISCORD_TOKEN is not set");
  process.exit(1);
}
const workerUrl = process.argv[2];

const MANAGE_SERVER = String(1 << 5);
const nameOption = (name, description, autocomplete = false) =>
  ({ type: 3, name, description, required: true, ...(autocomplete ? { autocomplete: true } : {}) });
const group = (name, description, item, what, addHelp, removeHelp, listHelp) => ({
  name, description, default_member_permissions: MANAGE_SERVER, contexts: [0], integration_types: [0],
  options: [
    { type: 1, name: "add", description: addHelp, options: [nameOption(item, `${what} username or link`)] },
    { type: 1, name: "remove", description: removeHelp, options: [nameOption(item, `${what} username`, true)] },
    { type: 1, name: "list", description: listHelp },
  ],
});
const COMMANDS = [
  group("twitch", "Lil Kifu's Twitch live notifications", "streamer", "Twitch",
    "Announce when this Twitch streamer goes live", "Stop announcing this Twitch streamer",
    "Show the Twitch streamers Lil Kifu announces"),
  group("tiktok", "Lil Nao's TikTok upload notifications", "account", "TikTok",
    "Announce new uploads from this TikTok account", "Stop announcing this TikTok account",
    "Show the TikTok accounts Lil Nao announces"),
];

async function discord(method, path, body) {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: {
      Authorization: `Bot ${token}`,
      "Content-Type": "application/json",
      "User-Agent": "DiscordBot (https://github.com/Hayone31/lil-kifu-notifier, 1.0)",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${path} -> HTTP ${response.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

const app = await discord("GET", "/applications/@me");
console.log(`App: ${app.name} (${app.id})`);

const registered = await discord("PUT", `/applications/${app.id}/commands`, COMMANDS);
console.log(`Registered: ${registered.map((command) => `/${command.name}`).join(", ")}`);

for (const guild of await discord("GET", "/users/@me/guilds")) {
  const old = await discord("GET", `/applications/${app.id}/guilds/${guild.id}/commands`);
  if (old.length) {
    await discord("PUT", `/applications/${app.id}/guilds/${guild.id}/commands`, []);
    console.log(`Removed ${old.length} old command(s) from ${guild.name}`);
  }
}

if (workerUrl) {
  await discord("PATCH", "/applications/@me", { interactions_endpoint_url: workerUrl });
  console.log(`Discord now sends commands to ${workerUrl}`);
}
console.log(`Add the commands to a server: https://discord.com/oauth2/authorize?client_id=${app.id}&scope=applications.commands`);
