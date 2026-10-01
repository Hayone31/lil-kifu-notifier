// Lil Kifu panel: Discord slash commands that edit the Twitch and TikTok lists in config.toml,
// plus a 5-minute timer that starts the notification check on GitHub.
//
// Discord sends every command here as a signed HTTP request. The answer goes back in the HTTP
// response, so this Worker never calls Discord itself. Changes are committed to the GitHub repo,
// and that commit starts a notification check right away. GitHub's own scheduler is too unreliable
// for 5-minute checks, so the timer asks GitHub to run the check instead.
import { parseTikTokHandle, parseTwitchLogin, readList, writeList } from "./lists.js";
import { createConnectLink, finishConnect, isConnected, latestVideos, privacyPage, startConnect, termsPage } from "./tiktok.js";

const PING = 1, COMMAND = 2, AUTOCOMPLETE = 4;
const PONG = 1, MESSAGE = 4, CHOICES = 8;
const EPHEMERAL = 64;
const ADMIN_BITS = (1n << 3n) | (1n << 5n); // Administrator, Manage Server
const LOOKUP_MS = 1800; // Discord wants an answer within 3 seconds

const LISTS = {
  twitch: { section: "twitch", key: "streamers", parse: parseTwitchLogin, bot: "Lil Kifu", site: "Twitch", what: "Twitch streamer" },
  tiktok: { section: "tiktok", key: "accounts", parse: parseTikTokHandle, bot: "Lil Nao", site: "TikTok", what: "TikTok account" },
};

export default {
  async fetch(request, env, ctx) {
    if (request.method === "GET") {
      const path = new URL(request.url).pathname;
      const route = PAGES[path];
      // The TikTok login pages only exist once a TikTok app is set up (see README).
      if (route && path.startsWith("/tiktok/") && !env.TIKTOK_CLIENT_KEY) return new Response("Not found", { status: 404 });
      return route ? route(request, env) : new Response("Lil Kifu panel is running.");
    }
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
    const body = await request.text();
    const valid = await verifySignature(env.DISCORD_PUBLIC_KEY, request.headers.get("X-Signature-Ed25519"),
      request.headers.get("X-Signature-Timestamp"), body);
    if (!valid) return new Response("invalid request signature", { status: 401 });
    const interaction = JSON.parse(body);
    if (interaction.type === PING) return json({ type: PONG });
    try {
      const work = handle(interaction, env, new URL(request.url).origin);
      const finished = work.catch((error) => console.error(error));
      let timer;
      const late = new Promise((resolve) => { timer = setTimeout(() => resolve(null), Number(env.REPLY_DEADLINE_MS) || 2500); });
      const reply = await Promise.race([work, late]);
      clearTimeout(timer);
      if (reply) return json(reply);
      // Discord stops waiting after 3 seconds. Let the change finish in the background and say so,
      // instead of Discord showing "interaction failed" for a change that still goes through.
      ctx?.waitUntil?.(finished);
      return json(interaction.type === AUTOCOMPLETE ? choices([]) : message(
        `GitHub is slow right now, so this is still being saved. Check in a moment with \`/${interaction.data?.name ?? "twitch"} list\`.`));
    } catch (error) {
      console.error(error);
      return json(interaction.type === AUTOCOMPLETE ? choices([]) : message(`Something went wrong: ${error.message}`));
    }
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(startCheck(env));
  },
};

const PAGES = {
  "/tiktok/connect": startConnect,
  "/tiktok/callback": finishConnect,
  "/tiktok/videos": latestVideos,
  "/terms": termsPage,
  "/privacy": privacyPage,
};

/** Ask GitHub to run the notification workflow now. "timer" keeps the TikTok checks at their own pace. */
export async function startCheck(env) {
  const response = await github(env, "actions/workflows/notify.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: { source: "timer" } }),
  });
  if (!response.ok) console.error(`Couldn't start the check: GitHub answered ${response.status} ${await response.text()}`);
  return response.ok;
}

export async function handle(interaction, env, origin) {
  const isAutocomplete = interaction.type === AUTOCOMPLETE;
  const allowed = String(env.ALLOWED_GUILD_IDS || "").split(",").map((id) => id.trim()).filter(Boolean);
  if (!interaction.guild_id || !allowed.includes(interaction.guild_id)) {
    return isAutocomplete ? choices([]) : message(`Lil Kifu isn't set up for this server yet (server ID ${interaction.guild_id ?? "none"}).`);
  }
  if (!isAdmin(interaction)) {
    return isAutocomplete ? choices([]) : message("Only server admins can change these lists.");
  }
  const list = LISTS[interaction.data?.name];
  const sub = interaction.data?.options?.[0];
  if (!list || !sub || (interaction.type !== COMMAND && !isAutocomplete)) return message("Unknown command.");
  const input = String(sub.options?.[0]?.value ?? "");
  if (isAutocomplete) return suggest(list, input, env);
  if (sub.name === "list") return showList(list, env);
  if (sub.name === "add") return add(list, input, env);
  if (sub.name === "remove") return remove(list, input, env);
  if (sub.name === "connect" && list === LISTS.tiktok) return connect(input, env, origin);
  return message("Unknown command.");
}

async function connect(input, env, origin) {
  const handle = parseTikTokHandle(input);
  if (!handle) return message(`**${escape(input)}** doesn't look like a TikTok account name.`);
  const link = await createConnectLink(env, origin, handle);
  return message(
    `Send this link to whoever manages **@${escape(handle)}**:\n${link}\n\n` +
    "They log in to TikTok as that account and allow access once. After that, Lil Nao posts each upload with its caption, " +
    `cover and a direct link. The link works once and expires in 24 hours. Add the account with \`/tiktok add\` too if it isn't on the list yet.`);
}

async function add(list, input, env) {
  const name = list.parse(input);
  if (!name) return message(`**${escape(input)}** doesn't look like a ${list.what} name.`);
  const [found, file] = await Promise.all([lookup(list, name, env), getFile(env, "config.toml")]);
  if (found.exists === false) return message(`There's no ${list.what} called **${escape(name)}**.`);
  const shown = list === LISTS.tiktok ? `@${name}` : found.display || name;
  const result = await changeList(env, list, file, (items) =>
    items.some((item) => item.toLowerCase() === name.toLowerCase()) ? null : [...items, name],
  `${list.site}: add ${name} (from Discord)`);
  if (!result.changed) return message(`**${escape(shown)}** is already on ${list.bot}'s list.`);
  const unchecked = found.exists === null
    ? ` (I couldn't check the name on ${list.site} just now, so double-check the spelling.)` : "";
  const next = list === LISTS.twitch
    ? "A check runs right away, so if they're live the notification shows up within a couple of minutes."
    : "The first check only notes how many videos they have. Every upload after that gets announced (checked about every 15 minutes).";
  return message(`Added **${escape(shown)}** to ${list.bot}'s ${list.site} list. ${next}${unchecked}`);
}

async function remove(list, input, env) {
  const name = (list.parse(input) || input.trim()).toLowerCase();
  const result = await changeList(env, list, null, (items) => {
    const kept = items.filter((item) => item.toLowerCase() !== name);
    return kept.length === items.length ? null : kept;
  }, `${list.site}: remove ${name} (from Discord)`);
  if (!result.changed) return message(`**${escape(input.trim())}** isn't on ${list.bot}'s list.`);
  const after = list === LISTS.twitch
    ? ` If they're live right now, that notification still switches to "ended" when the stream is over.` : "";
  return message(`Removed **${escape(name)}** from ${list.bot}'s ${list.site} list.${after}`);
}

async function showList(list, env) {
  const [config, state] = await Promise.all([getFile(env, "config.toml"), getFile(env, "state.json")]);
  const items = readList(config.text, list.section, list.key);
  if (!items.length) return message(`${list.bot}'s ${list.site} list is empty. Add one with \`/${list.section} add\`.`);
  let saved = {};
  try { saved = JSON.parse(state.text || "{}"); } catch { /* an unreadable state only hides the status column */ }
  const connected = list === LISTS.tiktok && env.TIKTOK
    ? new Set((await Promise.all(items.map(async (item) => (await isConnected(env, item)) ? item : null))).filter(Boolean))
    : new Set();
  const lines = items.map((item) => {
    if (list === LISTS.twitch) {
      const live = Object.values(saved.streams || {}).some((entry) => entry.login === item.toLowerCase());
      return `• [${escape(item)}](<https://www.twitch.tv/${item.toLowerCase()}>)${live ? " · 🔴 live" : ""}`;
    }
    const count = saved.tiktok?.[item.toLowerCase()]?.video_count;
    const videos = count === undefined ? "" : ` · ${count} video${count === 1 ? "" : "s"}`;
    const official = connected.has(item) ? " · connected to TikTok" : "";
    return `• [@${escape(item)}](<https://www.tiktok.com/@${item.toLowerCase()}>)${videos}${official}`;
  });
  return message(`**${list.bot}'s ${list.site} list (${items.length})**\n${lines.join("\n")}`.slice(0, 2000));
}

async function suggest(list, input, env) {
  const items = readList((await getFile(env, "config.toml")).text, list.section, list.key);
  const typed = input.trim().toLowerCase().replace(/^@/, "");
  return choices(items.filter((item) => item.toLowerCase().includes(typed)).slice(0, 25)
    .map((item) => ({ name: item, value: item })));
}

/** Read-modify-write config.toml; retries when someone else committed in between. */
async function changeList(env, list, file, change, commitMessage) {
  for (let attempt = 0; attempt < 3; attempt++) {
    file = attempt === 0 && file ? file : await getFile(env, "config.toml");
    const items = readList(file.text, list.section, list.key);
    const next = change(items);
    if (!next) return { changed: false, items };
    if (await putFile(env, "config.toml", writeList(file.text, list.section, list.key, next), file.sha, commitMessage)) {
      return { changed: true, items: next };
    }
  }
  throw new Error("the list was changed at the same moment, please try again");
}

/** Does the account exist? exists is null when the site couldn't be asked in time. */
async function lookup(list, name, env) {
  const ask = async () => {
    if (list === LISTS.twitch) {
      const user = await twitchUser(env, name);
      return user ? { exists: true, display: user.display_name } : { exists: false };
    }
    const profile = `https://www.tiktok.com/@${name}`;
    const response = await fetch(`https://www.tiktok.com/oembed?url=${encodeURIComponent(profile)}`);
    if (response.status === 400 || response.status === 404) return { exists: false };
    return { exists: response.ok ? true : null };
  };
  let timer;
  const timeout = new Promise((resolve) => { timer = setTimeout(() => resolve({ exists: null }), LOOKUP_MS); });
  try {
    return await Promise.race([ask(), timeout]);
  } catch {
    return { exists: null };
  } finally {
    clearTimeout(timer);
  }
}

// --- GitHub -------------------------------------------------------------------------------------

function github(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "User-Agent": "lil-kifu-panel",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  });
}

function checkToken(response) {
  if (response.status === 401 || response.status === 403) {
    throw new Error("GitHub refused GITHUB_TOKEN (it may have expired). The owner needs to renew it in Cloudflare");
  }
}

async function getFile(env, path) {
  const response = await github(env, `contents/${path}?ref=main`);
  checkToken(response);
  if (response.status === 404) return { text: "", sha: undefined };
  if (!response.ok) throw new Error(`GitHub answered ${response.status}`);
  const data = await response.json();
  return { text: fromBase64(data.content), sha: data.sha };
}

async function putFile(env, path, text, sha, commitMessage) {
  if (!env.COMMIT_EMAIL) throw new Error("COMMIT_EMAIL isn't set, so the commit would show the owner's real email");
  // Without an explicit identity GitHub signs API commits with the account's primary email, and this repo is public.
  const identity = { name: "Lil Kifu panel", email: env.COMMIT_EMAIL };
  const response = await github(env, `contents/${path}`, {
    method: "PUT",
    body: JSON.stringify({ message: commitMessage, content: toBase64(text), sha, branch: "main", author: identity, committer: identity }),
  });
  checkToken(response);
  if (response.status === 409 || response.status === 422) return false; // changed by someone else first
  if (!response.ok) throw new Error(`GitHub answered ${response.status}`);
  return true;
}

function fromBase64(encoded) {
  const binary = atob(encoded.replace(/\s/g, ""));
  return new TextDecoder().decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
}

function toBase64(text) {
  let binary = "";
  for (const byte of new TextEncoder().encode(text)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

// --- Twitch -------------------------------------------------------------------------------------

let twitchToken = null; // reused while this Worker instance stays warm

async function twitchUser(env, login) {
  for (let attempt = 0; attempt < 2; attempt++) {
    twitchToken ??= await twitchAppToken(env);
    const response = await fetch(`https://api.twitch.tv/helix/users?login=${encodeURIComponent(login)}`, {
      headers: { "Client-Id": env.TWITCH_CLIENT_ID, Authorization: `Bearer ${twitchToken}` },
    });
    if (response.status === 401) { twitchToken = null; continue; }
    if (!response.ok) throw new Error(`Twitch answered ${response.status}`);
    return (await response.json()).data?.[0] ?? null;
  }
  throw new Error("Twitch keeps rejecting the app token");
}

async function twitchAppToken(env) {
  const response = await fetch("https://id.twitch.tv/oauth2/token", {
    method: "POST",
    body: new URLSearchParams({ client_id: env.TWITCH_CLIENT_ID, client_secret: env.TWITCH_CLIENT_SECRET, grant_type: "client_credentials" }),
  });
  if (!response.ok) throw new Error(`Twitch login failed (${response.status})`);
  return (await response.json()).access_token;
}

// --- Discord ------------------------------------------------------------------------------------

let cachedKey = { hex: null, key: null };

export async function verifySignature(publicKeyHex, signatureHex, timestamp, body) {
  if (!publicKeyHex || !signatureHex || !timestamp) return false;
  // A genuine request is fresh; a captured one replayed later is not.
  if (!(Math.abs(Date.now() / 1000 - Number(timestamp)) <= 300)) return false;
  try {
    if (cachedKey.hex !== publicKeyHex) {
      cachedKey = { hex: publicKeyHex, key: await crypto.subtle.importKey("raw", hexToBytes(publicKeyHex), { name: "Ed25519" }, false, ["verify"]) };
    }
    return await crypto.subtle.verify("Ed25519", cachedKey.key, hexToBytes(signatureHex), new TextEncoder().encode(timestamp + body));
  } catch {
    return false;
  }
}

function hexToBytes(hex) {
  return Uint8Array.from(hex.match(/../g) ?? [], (pair) => parseInt(pair, 16));
}

function isAdmin(interaction) {
  try {
    return (BigInt(interaction.member?.permissions ?? "0") & ADMIN_BITS) !== 0n;
  } catch {
    return false;
  }
}

function message(content) {
  return { type: MESSAGE, data: { content, flags: EPHEMERAL, allowed_mentions: { parse: [] } } };
}

function choices(list) {
  return { type: CHOICES, data: { choices: list } };
}

function json(data) {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}

function escape(text) {
  return String(text).replace(/([\\*_~`|>])/g, "\\$1");
}
