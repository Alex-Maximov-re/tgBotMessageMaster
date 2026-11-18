"""Связка вебхука Авито, опроса чатов и отправки в Telegram."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.avito import AvitoClient, AvitoError
from app.config import Settings
from app.messages import (
    IncomingMessage,
    as_int,
    brief_event,
    format_notification,
    is_historical,
    is_outgoing,
    parse_chat_list,
    parse_webhook,
)
from app.store import SeenStore
from app.telegram import TelegramClient

log = logging.getLogger("avito_notify")


class Service:
    def __init__(
        self,
        settings: Settings,
        *,
        avito: AvitoClient | None = None,
        telegram: TelegramClient | None = None,
        store: SeenStore | None = None,
    ):
        self.settings = settings
        self.avito = avito
        self.telegram = telegram
        self.store = store or SeenStore(Path(settings.state_path))
        self._account_user_id = settings.avito_user_id
        self._http: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._started_at = time.time()

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20)
        if self.avito is None:
            self.avito = AvitoClient(
                self.settings.avito_client_id,
                self.settings.avito_client_secret,
                self._http,
            )
        if self.telegram is None:
            self.telegram = TelegramClient(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
                self._http,
            )
        try:
            user_id = await self._resolve_user_id()
            log.info("Аккаунт Авито user_id=%s", user_id)
        except Exception:
            log.exception(
                "Не удалось получить user_id Авито. "
                "Входящий вебхук всё равно принимается, название объявления может не подтянуться."
            )
        if self.settings.subscribe_on_start:
            await self._subscribe_if_configured()
        if self.settings.poll_interval_seconds > 0:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def subscribe(self) -> str:
        if self.avito is None:
            raise RuntimeError("Клиент Авито не запущен")
        url = self.settings.public_webhook_url()
        result = await self.avito.subscribe(url)
        log.info("Вебхук Авито зарегистрирован на %s", self.settings.public_base_url)
        log.info("Ответ подписки: %s", result)
        return url

    async def handle_webhook(self, body: dict[str, Any]) -> None:
        message = parse_webhook(body)
        if message is None:
            log.info("Вебхук без сообщения: %s", brief_event(body))
            return
        await self._deliver(message, None)

    async def poll_once(self, *, seed_only: bool) -> int:
        if self.avito is None:
            raise RuntimeError("Клиент Авито не запущен")
        user_id = await self._resolve_user_id()
        payload = await self.avito.unread_chats(user_id)
        found = parse_chat_list(payload)
        delivered = 0
        remembered = 0
        for message, chat in found:
            if seed_only and is_historical(message, self._started_at):
                if self.store.claim(message.message_id):
                    remembered += 1
                continue
            await self._deliver(message, chat)
            delivered += 1
        if seed_only:
            log.info(
                "Первый опрос: старых непрочитанных %s, новых в обработке %s",
                remembered,
                delivered,
            )
        return delivered

    async def _deliver(
        self,
        message: IncomingMessage,
        chat: dict[str, Any] | None,
    ) -> None:
        if self.telegram is None:
            raise RuntimeError("Клиент Telegram не запущен")
        owner = self._account_user_id
        if is_outgoing(message, owner, notify_own=self.settings.notify_own_messages):
            self.store.claim(message.message_id)
            log.info("Своё сообщение %s, без уведомления", message.message_id)
            return
        if not self.store.claim(message.message_id):
            log.info("Сообщение %s уже обработано", message.message_id)
            return
        try:
            details = chat
            if details is None and self.avito is not None and message.chat_id:
                details = await self._load_chat(message)
            text = format_notification(message, details)
            await self.telegram.send(text)
            log.info("Уведомление отправлено, чат %s", message.chat_id)
        except Exception:
            self.store.release(message.message_id)
            log.exception("Не удалось отправить уведомление %s", message.message_id)

    async def _load_chat(self, message: IncomingMessage) -> dict[str, Any] | None:
        assert self.avito is not None
        try:
            user_id = message.account_user_id or await self._resolve_user_id()
        except AvitoError:
            log.warning("Нет user_id, отправляю уведомление без карточки чата")
            return None
        try:
            return await self.avito.get_chat(user_id, message.chat_id)
        except Exception:
            log.warning(
                "Не удалось прочитать чат %s, отправляю текст как есть",
                message.chat_id,
                exc_info=True,
            )
            return None

    async def _resolve_user_id(self) -> int:
        if self._account_user_id is not None:
            return self._account_user_id
        if self.avito is None:
            raise AvitoError(0, "клиент не создан", "GET", "/core/v1/accounts/self")
        profile = await self.avito.get_self()
        user_id = as_int(profile.get("id"))
        if user_id is None:
            raise AvitoError(
                0,
                f"в ответе нет id: {profile}",
                "GET",
                "/core/v1/accounts/self",
            )
        self._account_user_id = user_id
        return user_id

    async def _subscribe_if_configured(self) -> None:
        if not self.settings.public_base_url:
            log.warning(
                "PUBLIC_BASE_URL пуст, вебхук в Авито не регистрирую. "
                "Для проверки без туннеля поставьте POLL_INTERVAL_SECONDS=30."
            )
            return
        try:
            await self.subscribe()
        except Exception:
            log.exception("Не удалось зарегистрировать вебхук Авито")

    async def _poll_loop(self) -> None:
        interval = max(5, self.settings.poll_interval_seconds)
        seed = self.store.empty and not self.settings.poll_notify_existing
        while True:
            try:
                await self.poll_once(seed_only=seed)
                seed = False
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Опрос чатов Авито не удался")
            await asyncio.sleep(interval)
