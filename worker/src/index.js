const FOOTERS = [
  "отписаться",
  "вы получили это письмо",
  "это письмо отправлено автоматически",
  "чтобы отключить уведомления",
  "управление уведомлениями",
];

export default {
  async fetch() {
    return new Response("avito mail worker", { status: 200 });
  },

  async email(message, env) {
    const subject = decodeHeader(message.headers.get("subject") || "(без темы)");
    const from = decodeHeader(message.headers.get("from") || message.from || "");
    const rawBytes = new Uint8Array(await new Response(message.raw).arrayBuffer());
    const raw = bytesToLatin1(rawBytes);
    const text = extractVisibleText(raw);
    const letter = [`Новое письмо`, `От: ${from}`, `Тема: ${subject}`, "", text || "(пустое письмо)"].join("\n");
    await sendTelegram(env, letter);
  },
};

export function extractVisibleText(raw) {
  const part = readPart(raw);
  const source = tidy(part.plain) || tidy(stripHtml(part.html));
  return source;
}

export function decodeHeader(value) {
  return value.replace(/=\?([^?]+)\?([BbQq])\?([\s\S]*?)\?=/g, (_, charset, encoding, data) => {
    const bytes = encoding.toLowerCase() === "b"
      ? base64ToBytes(data)
      : qpToBytes(data.replace(/_/g, " "));
    return decodeBytes(bytes, charset);
  });
}

async function sendTelegram(env, text) {
  const token = env.TELEGRAM_BOT_TOKEN;
  const chatId = env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) {
    throw new Error("Задайте секреты TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID");
  }
  for (let offset = 0; offset < text.length; offset += 3900) {
    const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: text.slice(offset, offset + 3900),
        disable_web_page_preview: true,
      }),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(`Telegram ${response.status}: ${detail.slice(0, 300)}`);
    }
  }
}

function readPart(raw) {
  const { headers, body } = splitHeaders(raw);
  const type = headerValue(headers, "content-type") || "text/plain";
  const encoding = headerValue(headers, "content-transfer-encoding");
  const bytes = decodeTransfer(body, encoding);
  const media = type.split(";")[0].trim().toLowerCase();
  const boundary = /boundary="?([^";]+)"?/i.exec(type);
  if (media.startsWith("multipart/") && boundary) {
    const combined = { plain: "", html: "" };
    for (const piece of splitMultipart(bytes, boundary[1])) {
      const nested = readPart(piece);
      if (!combined.plain && nested.plain) combined.plain = nested.plain;
      if (!combined.html && nested.html) combined.html = nested.html;
    }
    return combined;
  }
  const text = decodeBytes(bytes, charsetOf(type));
  if (media === "text/html") return { plain: "", html: text };
  if (media.startsWith("text/")) return { plain: text, html: "" };
  return { plain: "", html: "" };
}

function splitHeaders(raw) {
  const match = /\r?\n\r?\n/.exec(raw);
  if (!match) return { headers: raw, body: "" };
  return {
    headers: raw.slice(0, match.index),
    body: raw.slice(match.index + match[0].length),
  };
}

function headerValue(headers, name) {
  const lines = headers.split(/\r?\n/);
  const unfolded = [];
  for (const line of lines) {
    if (/^[ \t]/.test(line) && unfolded.length) unfolded[unfolded.length - 1] += ` ${line.trim()}`;
    else unfolded.push(line);
  }
  const prefix = `${name.toLowerCase()}:`;
  const found = unfolded.find((line) => line.toLowerCase().startsWith(prefix));
  return found ? found.slice(prefix.length).trim() : "";
}

function charsetOf(type) {
  const match = /charset="?([^";]+)"?/i.exec(type);
  return match ? match[1].trim() : "utf-8";
}

function decodeTransfer(body, encoding) {
  const normalized = (encoding || "").toLowerCase().trim();
  if (normalized === "base64") return base64ToBytes(body);
  if (normalized === "quoted-printable") return qpToBytes(body);
  return latin1Bytes(body);
}

function splitMultipart(bytes, boundary) {
  const text = bytesToLatin1(bytes);
  const marker = `--${boundary}`;
  const parts = [];
  for (const chunk of text.split(marker).slice(1)) {
    if (chunk.startsWith("--")) break;
    const piece = chunk.replace(/^\r?\n/, "").replace(/\r?\n$/, "");
    if (piece.trim()) parts.push(piece);
  }
  return parts;
}

function base64ToBytes(data) {
  const clean = data.replace(/\s/g, "");
  const binary = atob(clean);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function qpToBytes(data) {
  const stripped = data.replace(/=\r?\n/g, "");
  const bytes = [];
  for (let i = 0; i < stripped.length; i += 1) {
    if (stripped[i] === "=" && /^[0-9A-Fa-f]{2}$/.test(stripped.slice(i + 1, i + 3))) {
      bytes.push(parseInt(stripped.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      bytes.push(stripped.charCodeAt(i) & 0xff);
    }
  }
  return Uint8Array.from(bytes);
}

function bytesToLatin1(bytes) {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return text;
}

function latin1Bytes(text) {
  const bytes = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i += 1) bytes[i] = text.charCodeAt(i) & 0xff;
  return bytes;
}

function decodeBytes(bytes, charset) {
  try {
    return new TextDecoder(charset || "utf-8").decode(bytes);
  } catch {
    return new TextDecoder("utf-8").decode(bytes);
  }
}

function stripHtml(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)));
}

function tidy(text) {
  const lines = text
    .replace(/\u00a0/g, " ")
    .split(/\r?\n/)
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .filter(Boolean);
  let compact = lines.join("\n");
  const folded = compact.toLowerCase().replaceAll("ё", "е");
  let cut = compact.length;
  for (const marker of FOOTERS) {
    const index = folded.indexOf(marker);
    if (index !== -1) cut = Math.min(cut, index);
  }
  compact = compact.slice(0, cut).trim();
  if (compact.length > 1200) return `${compact.slice(0, 1200).trimEnd()}…`;
  return compact;
}
