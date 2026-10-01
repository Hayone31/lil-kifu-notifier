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

/** Where `key = [ ... ]` sits in text. Comments and quoted strings may contain brackets, so scan them properly. */
function findArray(text, key) {
  const head = new RegExp(`^([ \\t]*${key}[ \\t]*=[ \\t]*)\\[`, "m").exec(text);
  if (!head) return null;
  const items = [];
  for (let i = head.index + head[0].length; i < text.length; i++) {
    const char = text[i];
    if (char === "#") {
      const lineEnd = text.indexOf("\n", i);
      if (lineEnd < 0) return null;
      i = lineEnd;
    } else if (char === '"' || char === "'") {
      let end = i + 1;
      while (end < text.length && text[end] !== char && text[end] !== "\n") {
        end += char === '"' && text[end] === "\\" ? 2 : 1;
      }
      if (text[end] !== char) return null;
      items.push(text.slice(i + 1, end));
      i = end;
    } else if (char === "]") {
      return { start: head.index, prefix: head[1], end: i + 1, items };
    }
  }
  return null;
}

export function readList(toml, section, key) {
  const bounds = sectionBounds(toml, section);
  if (!bounds) return [];
  return findArray(toml.slice(...bounds), key)?.items ?? [];
}

const UNUSUAL_LAYOUT = "config.toml has a layout the panel can't edit safely. Edit it on GitHub instead";

export function writeList(toml, section, key, items) {
  const array = items.length ? `[\n${items.map((item) => `    "${item}",`).join("\n")}\n]` : "[]";
  const bounds = sectionBounds(toml, section);
  let updated;
  if (!bounds) {
    updated = `${toml.replace(/\s*$/, "\n")}\n[${section}]\n${key} = ${array}\n`;
  } else {
    const [start, end] = bounds;
    const text = toml.slice(start, end);
    const found = findArray(text, key);
    if (!found && new RegExp(`^[ \\t]*${key}[ \\t]*=`, "m").test(text)) throw new Error(UNUSUAL_LAYOUT);
    const replaced = found
      ? text.slice(0, found.start) + found.prefix + array + text.slice(found.end)
      : `\n${key} = ${array}${text}`;
    updated = toml.slice(0, start) + replaced + toml.slice(end);
  }
  // Never commit a file that doesn't read back exactly as intended: a broken config.toml stops every notification.
  const check = readList(updated, section, key);
  if (check.length !== items.length || check.some((item, index) => item !== items[index])) throw new Error(UNUSUAL_LAYOUT);
  return updated;
}
