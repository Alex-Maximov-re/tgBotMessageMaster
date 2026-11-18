"""Отправка уведомлений через Bot API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("avito_notify.telegram")


class TelegramError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        preview = " ".join(body.split())[:500]
        super().__init__(f"Telegram sendMessage → {status}: {preview}")


class TelegramClient:
    def __init__(self, token: str, chat_id: str, http: httpx.AsyncClient):
        self._token = token
        self._chat_id = chat_id
        self._http = http

    async def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        response = await self._post(url, payload)
        if response.status_code == 400 and "can't parse" in response.text.lower():
            log.warning("Telegram не принял HTML, отправляю как обычный текст")
            plain = dict(payload)
            plain.pop("parse_mode", None)
            response = await self._post(url, plain)
        if response.status_code >= 400:
            raise TelegramError(response.status_code, response.text)

    async def get_updates(self) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        response = await self._http.get(url, params={"limit": 20, "timeout": 0})
        if response.status_code >= 400:
            raise TelegramError(response.status_code, response.text)
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def _post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(3):
            response = await self._http.post(url, json=payload)
            last = response
            if response.status_code != 429 and response.status_code < 500:
                return response
            await asyncio.sleep(1 + attempt)
        assert last is not None
        return last


def chats_from_updates(payload: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    updates = payload.get("result")
    if not isinstance(updates, list):
        return found
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get("message") or update.get("edited_message") or {}
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            continue
        chat_id = str(chat["id"])
        if chat_id in seen:
            continue
        seen.add(chat_id)
        title = (
            chat.get("title")
            or chat.get("username")
            or chat.get("first_name")
            or ""
        )
        found.append((chat_id, str(title)))
    return found
