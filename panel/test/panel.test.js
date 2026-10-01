import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import worker, { startCheck } from "../src/index.js";
import { parseTikTokHandle, parseTwitchLogin, readList, writeList } from "../src/lists.js";

const CONFIG = `# Lil Kifu & Lil Nao settings.

[twitch]
# Lil Kifu announces when these Twitch streamers go live.
streamers = [
    "azimorning",
    "ohnepixel",
]
ping = ""
name = "Lil Kifu"

[tiktok]
# Lil Nao announces new posts from these TikTok accounts.
accounts = [
    "renaissance.guild",
]
ping = ""
name = "Lil Nao"
`;

// --- config.toml editing ------------------------------------------------------------------------

test("reads both lists", () => {
  assert.deepEqual(readList(CONFIG, "twitch", "streamers"), ["azimorning", "ohnepixel"]);
  assert.deepEqual(readList(CONFIG, "tiktok", "accounts"), ["renaissance.guild"]);
  assert.deepEqual(readList(CONFIG, "youtube", "channels"), []);
  assert.deepEqual(readList('[twitch]\nstreamers = ["a", \'b\'] # inline\n', "twitch", "streamers"), ["a", "b"]);
});

test("rewrites one list and leaves everything else alone", () => {
  const updated = writeList(CONFIG, "twitch", "streamers", ["azimorning", "ohnepixel", "newone"]);
  assert.deepEqual(readList(updated, "twitch", "streamers"), ["azimorning", "ohnepixel", "newone"]);
  assert.equal(updated.replace('    "newone",\n', ""), CONFIG);
  const emptied = writeList(CONFIG, "tiktok", "accounts", []);
  assert.match(emptied, /^accounts = \[\]$/m);
  assert.deepEqual(readList(emptied, "tiktok", "accounts"), []);
  assert.ok(emptied.includes('name = "Lil Nao"') && emptied.includes("# Lil Nao announces"));
  const added = writeList("[twitch]\nstreamers = []\n", "tiktok", "accounts", ["X"]);
  assert.deepEqual(readList(added, "tiktok", "accounts"), ["X"]);
});

test("parses names and links", () => {
  assert.equal(parseTwitchLogin("https://www.twitch.tv/OhnePixel"), "ohnepixel");
  assert.equal(parseTwitchLogin("@AziMorning "), "azimorning");
  assert.equal(parseTwitchLogin("two words"), null);
  assert.equal(parseTikTokHandle("https://www.tiktok.com/@Renaissance.Guild?lang=en"), "Renaissance.Guild");
  assert.equal(parseTikTokHandle("@a"), null);
});

// --- the Worker ---------------------------------------------------------------------------------

const keys = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const publicKey = Buffer.from(await crypto.subtle.exportKey("raw", keys.publicKey)).toString("hex");
const ENV = {
  DISCORD_PUBLIC_KEY: publicKey, ALLOWED_GUILD_IDS: "111, 222", GITHUB_REPO: "owner/repo",
  GITHUB_TOKEN: "gh-token", TWITCH_CLIENT_ID: "cid", TWITCH_CLIENT_SECRET: "secret",
};

let files, commits, dispatches, twitchUsers, tiktokStatus, conflicts, githubStatus;

beforeEach(() => {
  files = {
    "config.toml": CONFIG,
    "state.json": JSON.stringify({ streams: { 9: { login: "azimorning" } }, tiktok: { "renaissance.guild": { video_count: 1 } } }),
  };
  commits = [];
  dispatches = [];
  twitchUsers = { ohnepixel: "ohnePixel", azimorning: "AziMorning", newone: "NewOne" };
  tiktokStatus = 200;
  conflicts = 0;
  githubStatus = 200;
  const kv = new Map();
  ENV.TIKTOK = {
    store: kv,
    async get(key) { return kv.get(key) ?? null; },
    async put(key, value) { kv.set(key, value); },
    async delete(key) { kv.delete(key); },
  };
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const method = init.method ?? "GET";
    if (url.hostname === "api.github.com") {
      assert.equal(init.headers.Authorization, "Bearer gh-token");
      if (githubStatus !== 200) return new Response("{}", { status: githubStatus });
      if (url.pathname === "/repos/owner/repo/actions/workflows/notify.yml/dispatches") {
        assert.equal(method, "POST");
        dispatches.push(JSON.parse(init.body));
        return new Response(null, { status: 204 });
      }
      const path = url.pathname.split("/contents/")[1];
      if (method === "GET") {
        if (!(path in files)) return new Response("{}", { status: 404 });
        return Response.json({ content: Buffer.from(files[path]).toString("base64"), sha: `sha-${files[path].length}` });
      }
      const body = JSON.parse(init.body);
      if (conflicts > 0) {
        conflicts--;
        files[path] += "\n# someone else committed first\n";
        return new Response("{}", { status: 409 });
      }
      assert.equal(body.sha, `sha-${files[path].length}`);
      files[path] = Buffer.from(body.content, "base64").toString("utf8");
      commits.push(body.message);
      return Response.json({});
    }
    if (url.hostname === "id.twitch.tv") return Response.json({ access_token: "tok" });
    if (url.hostname === "api.twitch.tv") {
      const login = url.searchParams.get("login");
      return Response.json({ data: twitchUsers[login] ? [{ login, display_name: twitchUsers[login] }] : [] });
    }
    if (url.hostname === "www.tiktok.com") return new Response("{}", { status: tiktokStatus });
    throw new Error(`unexpected request to ${url}`);
  };
});

async function signed(interaction, { tamper = false } = {}) {
  const body = JSON.stringify(interaction);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = await crypto.subtle.sign("Ed25519", keys.privateKey, new TextEncoder().encode(timestamp + body));
  return new Request("https://panel.example/", {
    method: "POST",
    body: tamper ? body.replace("list", "add") : body,
    headers: { "X-Signature-Ed25519": Buffer.from(signature).toString("hex"), "X-Signature-Timestamp": timestamp },
  });
}

function command(group, sub, value, { guild = "111", permissions = "32", type = 2 } = {}) {
  const options = value === undefined ? [] : [{ name: "name", value, focused: type === 4 }];
  return { type, guild_id: guild, member: { permissions }, data: { name: group, options: [{ name: sub, type: 1, options }] } };
}

async function call(interaction) {
  const response = await worker.fetch(await signed(interaction), ENV);
  assert.equal(response.status, 200);
  return response.json();
}

const said = async (interaction) => (await call(interaction)).data.content;

test("rejects requests that Discord didn't sign", async () => {
  const response = await worker.fetch(await signed(command("twitch", "list"), { tamper: true }), ENV);
  assert.equal(response.status, 401);
  const unsigned = new Request("https://panel.example/", { method: "POST", body: "{}" });
  assert.equal((await worker.fetch(unsigned, ENV)).status, 401);
});

test("answers Discord's ping", async () => {
  assert.deepEqual(await call({ type: 1 }), { type: 1 });
});

test("only admins in allowed servers can use it", async () => {
  assert.match(await said(command("twitch", "list", undefined, { guild: "999" })), /isn't set up for this server yet \(server ID 999\)/);
  assert.match(await said(command("twitch", "list", undefined, { permissions: "2048" })), /Only server admins/);
  assert.match(await said(command("twitch", "list", undefined, { permissions: "8", guild: "222" })), /Lil Kifu's Twitch list \(2\)/);
  const reply = await call(command("twitch", "list"));
  assert.equal(reply.data.flags, 64); // only the admin sees the answer
  assert.deepEqual(reply.data.allowed_mentions, { parse: [] });
});

test("/twitch add", async () => {
  assert.match(await said(command("twitch", "add", "https://twitch.tv/NewOne")), /^Added \*\*NewOne\*\* to Lil Kifu's Twitch list\. A check runs right away/);
  assert.deepEqual(readList(files["config.toml"], "twitch", "streamers"), ["azimorning", "ohnepixel", "newone"]);
  assert.deepEqual(commits, ["Twitch: add newone (from Discord)"]);
  assert.match(await said(command("twitch", "add", "OhnePixel")), /\*\*ohnePixel\*\* is already on Lil Kifu's list/);
  assert.match(await said(command("twitch", "add", "nobody_like_this")), /There's no Twitch streamer called \*\*nobody\\_like\\_this\*\*/);
  assert.match(await said(command("twitch", "add", "not a name")), /doesn't look like a Twitch streamer name/);
  assert.equal(commits.length, 1);
});

test("/tiktok add keeps the spelling and checks the account", async () => {
  assert.match(await said(command("tiktok", "add", "@Some.Guild")), /^Added \*\*@Some.Guild\*\* to Lil Nao's TikTok list\. The first check only notes/);
  assert.deepEqual(readList(files["config.toml"], "tiktok", "accounts"), ["renaissance.guild", "Some.Guild"]);
  assert.match(await said(command("tiktok", "add", "some.guild")), /already on Lil Nao's list/);
  tiktokStatus = 400;
  assert.match(await said(command("tiktok", "add", "ghost.account")), /There's no TikTok account called/);
  tiktokStatus = 503;
  assert.match(await said(command("tiktok", "add", "maybe.real")), /couldn't check the name on TikTok just now/);
  assert.deepEqual(commits, ["TikTok: add Some.Guild (from Discord)", "TikTok: add maybe.real (from Discord)"]);
});

test("/twitch remove and /tiktok remove", async () => {
  assert.match(await said(command("twitch", "remove", "AziMorning")), /^Removed \*\*azimorning\*\* from Lil Kifu's Twitch list\. If they're live right now/);
  assert.deepEqual(readList(files["config.toml"], "twitch", "streamers"), ["ohnepixel"]);
  assert.match(await said(command("twitch", "remove", "stranger")), /\*\*stranger\*\* isn't on Lil Kifu's list/);
  assert.match(await said(command("tiktok", "remove", "Renaissance.Guild")), /^Removed \*\*renaissance.guild\*\* from Lil Nao's TikTok list\.$/);
  assert.deepEqual(readList(files["config.toml"], "tiktok", "accounts"), []);
  assert.ok(files["config.toml"].includes('name = "Lil Nao"'));
});

test("/twitch list and /tiktok list show live status and video counts", async () => {
  const twitch = await said(command("twitch", "list"));
  assert.match(twitch, /\[azimorning\]\(<https:\/\/www\.twitch\.tv\/azimorning>\) · 🔴 live/);
  assert.match(twitch, /\[ohnepixel\]\(<https:\/\/www\.twitch\.tv\/ohnepixel>\)$/m);
  assert.match(await said(command("tiktok", "list")), /\[@renaissance\.guild\]\(<https:\/\/www\.tiktok\.com\/@renaissance\.guild>\) · 1 video$/m);
  files["config.toml"] = writeList(CONFIG, "tiktok", "accounts", []);
  assert.match(await said(command("tiktok", "list")), /Lil Nao's TikTok list is empty\. Add one with `\/tiktok add`/);
});

test("/tiktok connect hands out a one-time link and /tiktok list shows connected accounts", async () => {
  const text = await said(command("tiktok", "connect", "@Renaissance.Guild"));
  const link = text.match(/https:\/\/panel\.example\/tiktok\/connect\?link=([0-9a-f]{32})/);
  assert.ok(link, text);
  assert.match(text, /^Send this link to whoever manages \*\*@Renaissance.Guild\*\*/);
  assert.equal(ENV.TIKTOK.store.get(`link:${link[1]}`), "Renaissance.Guild");
  assert.match(await said(command("tiktok", "connect", "no spaces allowed")), /doesn't look like a TikTok account name/);
  assert.doesNotMatch(await said(command("tiktok", "list")), /connected to TikTok/);
  ENV.TIKTOK.store.set("account:renaissance.guild", "{}");
  assert.match(await said(command("tiktok", "list")), /@renaissance\.guild.*· 1 video · connected to TikTok$/m);
});

test("remove offers the current list as suggestions", async () => {
  const reply = await call(command("twitch", "remove", "OHN", { type: 4 }));
  assert.deepEqual(reply, { type: 8, data: { choices: [{ name: "ohnepixel", value: "ohnepixel" }] } });
  const stranger = await call(command("twitch", "remove", "o", { type: 4, permissions: "0" }));
  assert.deepEqual(stranger.data.choices, []);
});

test("a commit by someone else in between is retried", async () => {
  conflicts = 1;
  assert.match(await said(command("twitch", "add", "newone")), /^Added/);
  assert.ok(files["config.toml"].includes("# someone else committed first"));
  assert.deepEqual(readList(files["config.toml"], "twitch", "streamers"), ["azimorning", "ohnepixel", "newone"]);
});

test("the 5-minute timer asks GitHub to run the check", async () => {
  const pending = [];
  await worker.scheduled({ cron: "*/5 * * * *" }, ENV, { waitUntil: (promise) => pending.push(promise) });
  assert.deepEqual(await Promise.all(pending), [true]);
  assert.deepEqual(dispatches, [{ ref: "main", inputs: { source: "timer" } }]);
  githubStatus = 403;
  const original = console.error;
  console.error = () => {};
  try {
    assert.equal(await startCheck(ENV), false);
  } finally {
    console.error = original;
  }
});

test("an expired GitHub token is explained", async () => {
  githubStatus = 401;
  assert.match(await said(command("twitch", "list")), /GitHub refused GITHUB_TOKEN \(it may have expired\)/);
});
