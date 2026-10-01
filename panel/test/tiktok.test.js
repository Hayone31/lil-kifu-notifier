import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import { createConnectLink, isConnected } from "../src/tiktok.js";

const ORIGIN = "https://panel.example";

function memoryKv() {
  const store = new Map();
  return {
    store,
    async get(key, type) {
      const value = store.get(key);
      return value === undefined ? null : type === "json" ? JSON.parse(value) : value;
    },
    async put(key, value) { store.set(key, value); },
    async delete(key) { store.delete(key); },
  };
}

let env, calls, loggedInAs, videos, issued, tokenError;

beforeEach(() => {
  env = { TIKTOK: memoryKv(), TIKTOK_CLIENT_KEY: "client-key", TIKTOK_CLIENT_SECRET: "client-secret" };
  calls = [];
  loggedInAs = "renaissance.guild";
  issued = 0;
  tokenError = null;
  videos = [
    { id: "7600000000000000002", video_description: "One More Level! #fy #soulslike", title: "", create_time: 1790870000,
      cover_image_url: "https://cdn.example/cover2.jpg?x-expires=1", share_url: "https://www.tiktok.com/@renaissance.guild/video/7600000000000000002" },
    { id: "7600000000000000001", video_description: "", title: "First!", create_time: 1790000000,
      cover_image_url: "https://cdn.example/cover1.jpg", share_url: "https://www.tiktok.com/@renaissance.guild/video/7600000000000000001" },
  ];
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    calls.push(url.pathname);
    if (url.pathname === "/v2/oauth/token/") {
      const form = new URLSearchParams(init.body);
      assert.equal(form.get("client_key"), "client-key");
      assert.equal(form.get("client_secret"), "client-secret");
      if (tokenError) return Response.json({ error: "invalid_grant", error_description: tokenError }, { status: 400 });
      if (form.get("grant_type") === "authorization_code") {
        assert.equal(form.get("code"), "the-code");
        assert.equal(form.get("redirect_uri"), `${ORIGIN}/tiktok/callback`);
      } else {
        assert.equal(form.get("grant_type"), "refresh_token");
        assert.equal(form.get("refresh_token"), `refresh-${issued}`);
      }
      issued += 1;
      return Response.json({ access_token: `access-${issued}`, expires_in: 86400, open_id: "open-1",
        refresh_token: `refresh-${issued}`, refresh_expires_in: 31536000, scope: "user.info.basic,video.list", token_type: "Bearer" });
    }
    if (url.pathname === "/v2/user/info/") {
      return Response.json({ data: { user: { open_id: "open-1", username: loggedInAs } }, error: { code: "ok" } });
    }
    if (url.pathname === "/v2/video/list/") {
      assert.equal(init.headers.Authorization, `Bearer access-${issued}`);
      assert.match(url.searchParams.get("fields"), /video_description/);
      return Response.json({ data: { videos, cursor: 0, has_more: false }, error: { code: "ok", message: "" } });
    }
    throw new Error(`unexpected request to ${url}`);
  };
});

const get = (path) => worker.fetch(new Request(`${ORIGIN}${path}`), env);

async function connect(state) {
  return get(`/tiktok/callback?code=the-code&scopes=video.list&state=${state}`);
}

test("a connect link sends the owner to TikTok's login with our callback", async () => {
  const link = await createConnectLink(env, ORIGIN, "renaissance.guild");
  const id = new URL(link).searchParams.get("link");
  assert.match(link, /^https:\/\/panel\.example\/tiktok\/connect\?link=[0-9a-f]{32}$/);
  const response = await get(new URL(link).pathname + new URL(link).search);
  assert.equal(response.status, 302);
  const target = new URL(response.headers.get("Location"));
  assert.equal(target.origin + target.pathname, "https://www.tiktok.com/v2/auth/authorize/");
  assert.equal(target.searchParams.get("client_key"), "client-key");
  assert.equal(target.searchParams.get("response_type"), "code");
  assert.equal(target.searchParams.get("scope"), "user.info.basic,user.info.profile,video.list");
  assert.equal(target.searchParams.get("redirect_uri"), `${ORIGIN}/tiktok/callback`);
  assert.equal(target.searchParams.get("state"), id);
});

test("only a live link can be used", async () => {
  for (const path of ["/tiktok/connect?link=nope", "/tiktok/connect?link=0123456789abcdef0123456789abcdef", `/tiktok/callback?code=x&state=nope`]) {
    const response = await get(path);
    assert.equal(response.status, 410);
    assert.match(await response.text(), /This link has expired/);
  }
});

test("connecting stores the tokens and uses the link up", async () => {
  const id = new URL(await createConnectLink(env, ORIGIN, "renaissance.guild")).searchParams.get("link");
  const response = await connect(id);
  assert.equal(response.status, 200);
  assert.match(await response.text(), /Lil Nao is connected to @renaissance\.guild/);
  const saved = JSON.parse(env.TIKTOK.store.get("account:renaissance.guild"));
  assert.equal(saved.access_token, "access-1");
  assert.equal(saved.refresh_token, "refresh-1");
  assert.ok(await isConnected(env, "Renaissance.Guild"));
  assert.equal(env.TIKTOK.store.has(`link:${id}`), false);
  assert.equal((await connect(id)).status, 410); // a second use fails
});

test("logging in as a different account is refused", async () => {
  const id = new URL(await createConnectLink(env, ORIGIN, "renaissance.guild")).searchParams.get("link");
  loggedInAs = "someone.else";
  const response = await connect(id);
  assert.equal(response.status, 403);
  assert.match(await response.text(), /This link is for @renaissance\.guild, but you logged in as @someone\.else/);
  assert.equal(await isConnected(env, "renaissance.guild"), false);
  assert.ok(env.TIKTOK.store.has(`link:${id}`)); // can retry with the right account
});

test("a denied login changes nothing", async () => {
  const id = new URL(await createConnectLink(env, ORIGIN, "renaissance.guild")).searchParams.get("link");
  const response = await get(`/tiktok/callback?error=access_denied&state=${id}`);
  assert.equal(response.status, 400);
  assert.equal(await isConnected(env, "renaissance.guild"), false);
});

test("the notifier gets the latest videos, cached for a while", async () => {
  assert.equal((await get("/tiktok/videos?account=renaissance.guild")).status, 404);
  assert.equal((await get("/tiktok/videos?account=bad name")).status, 400);
  const id = new URL(await createConnectLink(env, ORIGIN, "renaissance.guild")).searchParams.get("link");
  await connect(id);

  const response = await get("/tiktok/videos?account=Renaissance.Guild");
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { connected: true, videos: [
    { id: "7600000000000000002", caption: "One More Level! #fy #soulslike", cover: "https://cdn.example/cover2.jpg?x-expires=1",
      url: "https://www.tiktok.com/@renaissance.guild/video/7600000000000000002", created: 1790870000 },
    { id: "7600000000000000001", caption: "First!", cover: "https://cdn.example/cover1.jpg",
      url: "https://www.tiktok.com/@renaissance.guild/video/7600000000000000001", created: 1790000000 },
  ] });
  const before = calls.length;
  await get("/tiktok/videos?account=renaissance.guild");
  assert.equal(calls.length, before); // served from the cache
});

test("an expiring access token is refreshed and the new refresh token kept", async () => {
  const id = new URL(await createConnectLink(env, ORIGIN, "renaissance.guild")).searchParams.get("link");
  await connect(id);
  const account = JSON.parse(env.TIKTOK.store.get("account:renaissance.guild"));
  env.TIKTOK.store.set("account:renaissance.guild", JSON.stringify({ ...account, access_expires_at: Date.now() + 1000 }));
  assert.equal((await get("/tiktok/videos?account=renaissance.guild")).status, 200);
  const refreshed = JSON.parse(env.TIKTOK.store.get("account:renaissance.guild"));
  assert.equal(refreshed.access_token, "access-2");
  assert.equal(refreshed.refresh_token, "refresh-2");
  assert.equal(refreshed.connected_at, account.connected_at);
});

test("an expired approval is reported instead of crashing", async () => {
  const id = new URL(await createConnectLink(env, ORIGIN, "renaissance.guild")).searchParams.get("link");
  await connect(id);
  const account = JSON.parse(env.TIKTOK.store.get("account:renaissance.guild"));
  env.TIKTOK.store.set("account:renaissance.guild", JSON.stringify({ ...account, access_expires_at: 0, refresh_expires_at: 1 }));
  const original = console.error;
  console.error = () => {};
  try {
    const response = await get("/tiktok/videos?account=renaissance.guild");
    assert.equal(response.status, 502);
    assert.match((await response.json()).error, /reconnect with \/tiktok connect/);
  } finally {
    console.error = original;
  }
});

test("terms and privacy pages exist for TikTok's app review", async () => {
  for (const [path, title] of [["/terms", "Terms of Service"], ["/privacy", "Privacy Policy"]]) {
    const response = await get(path);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("Content-Type"), /text\/html/);
    assert.match(await response.text(), new RegExp(title));
  }
});
