import assert from "node:assert/strict";
import test from "node:test";

import { decodeHeader, extractVisibleText } from "./index.js";

function asMime(text) {
  let raw = "";
  for (const byte of Buffer.from(text, "utf8")) raw += String.fromCharCode(byte);
  return raw;
}

test("reads a plain avito letter and cuts the footer", () => {
  const raw = asMime([
    "From: Avito <noreply@avito.ru>",
    "Subject: =?UTF-8?B?0JLQsNC8INC90LDQv9C40YHQsNC70Lg=?=",
    "Content-Type: text/plain; charset=utf-8",
    "",
    "Анна: ещё продаёте?",
    "Отписаться от уведомлений",
  ].join("\r\n"));
  assert.equal(decodeHeader("=?UTF-8?B?0JLQsNC8INC90LDQv9C40YHQsNC70Lg=?="), "Вам написали");
  assert.equal(extractVisibleText(raw), "Анна: ещё продаёте?");
});

test("reads html from a base64 part", () => {
  const html = "<p>Цена &lt;1000&gt;</p><p>Отписаться</p>";
  const encoded = Buffer.from(html, "utf8").toString("base64");
  const raw = asMime([
    "From: noreply@avito.ru",
    "Subject: Новое сообщение",
    "MIME-Version: 1.0",
    'Content-Type: multipart/alternative; boundary="bound"',
    "",
    "--bound",
    "Content-Type: text/html; charset=utf-8",
    "Content-Transfer-Encoding: base64",
    "",
    encoded,
    "--bound--",
  ].join("\r\n"));
  assert.equal(extractVisibleText(raw), "Цена <1000>");
});
