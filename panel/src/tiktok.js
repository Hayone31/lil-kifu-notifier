// TikTok's official API for accounts whose owner approved Lil Nao once.
//
//   /tiktok connect (admin command) -> one-time link -> /tiktok/connect -> TikTok login
//   -> /tiktok/callback checks it's the right account and stores the tokens in KV.
//   /tiktok/videos?account=x gives the notifier the latest public videos (cached for 5 minutes).

const AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/";
const TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/";
const USER_URL = "https://open.tiktokapis.com/v2/user/info/?fields=open_id,username,display_name";
const VIDEOS_URL = "https://open.tiktokapis.com/v2/video/list/?fields=id,title,video_description,cover_image_url,share_url,create_time";
const SCOPES = "user.info.basic,user.info.profile,video.list";
const LINK_TTL = 24 * 3600; // seconds a connect link stays valid
const CACHE_TTL = 300;      // seconds a video list is reused
const HANDLE = /^[a-z0-9_.]{2,24}$/;
const LINK_ID = /^[0-9a-f]{32}$/;
const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" };

export async function createConnectLink(env, origin, handle) {
  const id = crypto.randomUUID().replaceAll("-", "");
  await env.TIKTOK.put(`link:${id}`, handle, { expirationTtl: LINK_TTL });
  return `${origin}/tiktok/connect?link=${id}`;
}

export async function isConnected(env, handle) {
  return (await env.TIKTOK.get(`account:${handle.toLowerCase()}`)) !== null;
}

/** Step 1: a one-time link from /tiktok connect sends its holder to TikTok's login page. */
export async function startConnect(request, env) {
  const url = new URL(request.url);
  const id = url.searchParams.get("link") ?? "";
  const handle = LINK_ID.test(id) ? await env.TIKTOK.get(`link:${id}`) : null;
  if (!handle) return expiredLink();
  const authorize = new URL(AUTHORIZE_URL);
  authorize.search = new URLSearchParams({
    client_key: env.TIKTOK_CLIENT_KEY,
    scope: SCOPES,
    response_type: "code",
    redirect_uri: `${url.origin}/tiktok/callback`,
    state: id,
  }).toString();
  return Response.redirect(authorize.toString(), 302);
}

/** Step 2: TikTok sends the user back here with a code, which is swapped for tokens. */
export async function finishConnect(request, env) {
  const url = new URL(request.url);
  const id = url.searchParams.get("state") ?? "";
  const handle = LINK_ID.test(id) ? await env.TIKTOK.get(`link:${id}`) : null;
  if (!handle) return expiredLink();
  if (url.searchParams.get("error") || !url.searchParams.get("code")) {
    return page("Access wasn't granted", "TikTok didn't give Lil Nao access, so nothing changed. You can open the same link again to retry.", 400);
  }
  let tokens, user;
  try {
    tokens = await tokenRequest(env, {
      code: url.searchParams.get("code"),
      grant_type: "authorization_code",
      redirect_uri: `${url.origin}/tiktok/callback`,
    });
    user = await whoIs(tokens.access_token);
  } catch (error) {
    console.error(error);
    return page("Something went wrong", `TikTok answered with an error (${error.message}). Open the same link again to retry.`, 502);
  }
  if ((user.username ?? "").toLowerCase() !== handle.toLowerCase()) {
    return page("Wrong TikTok account", `This link is for @${handle}, but you logged in as @${user.username ?? "unknown"}. ` +
      `Log out of TikTok, then open the same link again and log in as @${handle}.`, 403);
  }
  await saveAccount(env, handle, tokens);
  await env.TIKTOK.delete(`link:${id}`);
  return page(`Lil Nao is connected to @${handle}`, "New uploads will be posted with their caption, cover and a direct link. You can close this page. " +
    "To undo this at any time, remove Lil Nao under Settings and privacy → Security → Apps and services on TikTok.", 200);
}

/** For the notifier: the latest public videos of a connected account. 404 when it isn't connected. */
export async function latestVideos(request, env) {
  const handle = (new URL(request.url).searchParams.get("account") ?? "").toLowerCase();
  if (!HANDLE.test(handle)) return json({ error: "bad account name" }, 400);
  const cached = await env.TIKTOK.get(`videos:${handle}`);
  if (cached) return new Response(cached, { headers: JSON_HEADERS });
  const stored = await env.TIKTOK.get(`account:${handle}`, "json");
  if (!stored) return json({ connected: false }, 404);
  try {
    const account = await withFreshToken(env, stored);
    const body = JSON.stringify({ connected: true, videos: await listVideos(account.access_token) });
    await env.TIKTOK.put(`videos:${handle}`, body, { expirationTtl: CACHE_TTL });
    return new Response(body, { headers: JSON_HEADERS });
  } catch (error) {
    console.error(error);
    return json({ connected: true, error: error.message }, 502);
  }
}

async function withFreshToken(env, account) {
  if (account.access_expires_at - Date.now() > 5 * 60_000) return account;
  if (account.refresh_expires_at < Date.now()) {
    throw new Error("TikTok's approval expired. An admin needs to reconnect with /tiktok connect");
  }
  const tokens = await tokenRequest(env, { grant_type: "refresh_token", refresh_token: account.refresh_token });
  return saveAccount(env, account.handle, tokens, account);
}

async function saveAccount(env, handle, tokens, previous = {}) {
  const now = Date.now();
  const account = {
    handle,
    open_id: tokens.open_id ?? previous.open_id,
    access_token: tokens.access_token,
    access_expires_at: now + Number(tokens.expires_in ?? 86400) * 1000,
    // TikTok may hand out a new refresh token; the old one then stops working.
    refresh_token: tokens.refresh_token ?? previous.refresh_token,
    refresh_expires_at: tokens.refresh_expires_in ? now + Number(tokens.refresh_expires_in) * 1000 : previous.refresh_expires_at,
    connected_at: previous.connected_at ?? now,
  };
  await env.TIKTOK.put(`account:${handle.toLowerCase()}`, JSON.stringify(account));
  return account;
}

async function tokenRequest(env, params) {
  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache" },
    body: new URLSearchParams({ client_key: env.TIKTOK_CLIENT_KEY, client_secret: env.TIKTOK_CLIENT_SECRET, ...params }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error || !data.access_token) {
    throw new Error(data.error_description || data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function whoIs(accessToken) {
  const response = await fetch(USER_URL, { headers: { Authorization: `Bearer ${accessToken}` } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || (data.error && data.error.code !== "ok")) throw new Error(data.error?.message || `HTTP ${response.status}`);
  return data.data?.user ?? {};
}

async function listVideos(accessToken) {
  const response = await fetch(VIDEOS_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ max_count: 10 }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || (data.error && data.error.code !== "ok")) throw new Error(data.error?.message || `HTTP ${response.status}`);
  return (data.data?.videos ?? []).map((video) => ({
    id: String(video.id),
    caption: video.video_description || video.title || "",
    cover: video.cover_image_url || "",
    url: video.share_url || "",
    created: Number(video.create_time) || 0,
  }));
}

// --- Pages --------------------------------------------------------------------------------------

const REPO = "https://github.com/Hayone31/lil-kifu-notifier";

export function termsPage() {
  return page("Lil Nao – Terms of Service",
    "Lil Nao is a small, non-commercial tool run by a gaming community. When a TikTok account owner allows it, " +
    "Lil Nao reads that account's public profile and list of public videos and posts a notification about new uploads " +
    "in the community's Discord server. It never posts to TikTok, never changes anything on the account, and is provided " +
    `as is, without warranty. Using it means agreeing to these terms and to TikTok's own terms. The source code is public: ${REPO}`, 200);
}

export function privacyPage() {
  return page("Lil Nao – Privacy Policy",
    "What Lil Nao reads: the TikTok account's ID, username and display name, and the caption, cover image, link and upload " +
    "time of its public videos. What it stores: the access tokens TikTok issues, kept as private data in Cloudflare and used only " +
    "to read that list, plus a short-lived copy of the latest videos. Video details are shown in the community's Discord channel; " +
    "nothing is sold or shared with anyone else. The account owner can withdraw access at any time in TikTok under Settings and " +
    `privacy → Security → Apps and services, after which the stored tokens stop working. Questions: ${REPO}/issues`, 200);
}

function expiredLink() {
  return page("This link has expired", "Connect links work once and only for 24 hours. Ask a server admin for a new one (/tiktok connect).", 410);
}

function page(title, text, status) {
  const escapeHtml = (value) => value.replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]);
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:640px;margin:10vh auto;padding:0 20px;color:#1f2328;background:#fff}
h1{font-size:1.4rem;margin:0 0 .6em}@media(prefers-color-scheme:dark){body{color:#e6edf3;background:#0d1117}}</style></head>
<body><h1>${escapeHtml(title)}</h1><p>${escapeHtml(text)}</p></body></html>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}
