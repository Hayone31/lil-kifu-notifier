// Reads and rewrites the two lists in config.toml without touching anything else in the file.

const TWITCH_LOGIN = /^[A-Za-z0-9_]{1,25}$/;
const TIKTOK_HANDLE = /^[A-Za-z0-9_.]{2,24}$/;

/** "Name", "@Name" or a twitch.tv link -> lowercase login, or null. */
export function parseTwitchLogin(text) {
  let value = String(text).trim();
  const link = value.match(/twitch\.tv\/(?:popout\/)?([A-Za-z0-9_]+)/i);
  if (link) value = link[1];
  value = value.replace(/^@+/, "");
  return TWITCH_LOGIN.test(value) ? value.toLowerCase() : null;
}

/** "Name", "@Name" or a tiktok.com/@Name link -> handle with its spelling kept (it is shown in Discord), or null. */
export function parseTikTokHandle(text) {
  let value = String(text).trim();
  const link = value.match(/tiktok\.com\/@([A-Za-z0-9_.]+)/i);
  if (link) value = link[1];
  value = value.replace(/^@+/, "");
  return TIKTOK_HANDLE.test(value) ? value : null;
}

function sectionBounds(toml, section) {
  const header = new RegExp(`^\\[${section}\\][ \\t]*(?:#.*)?$`, "m").exec(toml);
  if (!header) return null;
  const start = header.index + header[0].length;
  const next = /^\[[^\]\n]+\][ \t]*(?:#.*)?$/m.exec(toml.slice(start));
  return [start, next ? start + next.index : toml.length];
}

function arrayPattern(key) {
  return new RegExp(`^([ \\t]*${key}[ \\t]*=[ \\t]*)\\[([^\\]]*)\\]`, "m");
}

export function readList(toml, section, key) {
  const bounds = sectionBounds(toml, section);
  if (!bounds) return [];
  const match = arrayPattern(key).exec(toml.slice(...bounds));
  if (!match) return [];
  const body = match[2].replace(/#[^\n]*/g, ""); // comments inside the array
  return [...body.matchAll(/"([^"]*)"|'([^']*)'/g)].map((m) => m[1] ?? m[2]);
}

export function writeList(toml, section, key, items) {
  const array = items.length ? `[\n${items.map((item) => `    "${item}",`).join("\n")}\n]` : "[]";
  const bounds = sectionBounds(toml, section);
  if (!bounds) return `${toml.replace(/\s*$/, "\n")}\n[${section}]\n${key} = ${array}\n`;
  const [start, end] = bounds;
  const text = toml.slice(start, end);
  const match = arrayPattern(key).exec(text);
  const updated = match
    ? text.slice(0, match.index) + match[1] + array + text.slice(match.index + match[0].length)
    : `\n${key} = ${array}${text}`;
  return toml.slice(0, start) + updated + toml.slice(end);
}
