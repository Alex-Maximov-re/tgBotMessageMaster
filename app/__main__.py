"""Команды: serve, check, subscribe, mail, mail-check."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx
import uvicorn

from app.avito import AvitoClient, AvitoError
from app.config import Settings
from app.mail import MailPoller, MailboxError, fetch_letters, mailbox_hint, should_notify
from app.store import SeenStore
from app.telegram import TelegramClient, chats_from_updates
from app.web import create_app

log = logging.getLogger("avito_notify")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Уведомления о сообщениях Авито в Telegram"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "check", "subscribe", "mail", "mail-check"),
        default="serve",
        help="mail — опрос почты, mail-check — проверка ящика, serve — вебхук API",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.load()
    except SystemExit:
        raise
    if args.command == "serve":
        _serve(settings)
    elif args.command == "check":
        _run(check(settings))
    elif args.command == "subscribe":
        _run(subscribe(settings))
    elif args.command == "mail":
        _run(run_mail(settings))
    else:
        _run(mail_check(settings))


def _serve(settings: Settings) -> None:
    if settings.imap_user and not settings.avito_client_id:
        raise SystemExit("Для опроса почты запустите: python -m app mail")
    settings.require(
        "avito_client_id",
        "avito_client_secret",
        "telegram_bot_token",
        "telegram_chat_id",
    )
    settings.require_webhook_secret()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


async def check(settings: Settings) -> None:
    settings.require("avito_client_id", "avito_client_secret", "telegram_bot_token")
    async with httpx.AsyncClient(timeout=20) as http:
        avito = AvitoClient(
            settings.avito_client_id,
            settings.avito_client_secret,
            http,
        )
        try:
            profile = await avito.get_self()
        except AvitoError as exc:
            raise SystemExit(_hint(exc)) from exc
        user_id = profile.get("id")
        name = profile.get("name") or profile.get("email") or ""
        print(f"Авито отвечает. user_id={user_id} {name}".rstrip())
        try:
            subscriptions = await avito.subscriptions()
            print(f"Текущие подписки мессенджера: {subscriptions}")
        except AvitoError as exc:
            print(f"Список подписок пока недоступен: {_hint(exc)}")
        telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id, http)
        if settings.telegram_chat_id:
            await telegram.send(
                "Проверка: бот видит Авито и может писать в этот чат."
            )
            print("Telegram: тестовое сообщение отправлено.")
            return
        updates = await telegram.get_updates()
        chats = chats_from_updates(updates)
        if not chats:
            raise SystemExit(
                "TELEGRAM_CHAT_ID пуст. Напишите боту любое сообщение и снова запустите python -m app check."
            )
        print("TELEGRAM_CHAT_ID не задан. Подставьте один из чатов:")
        for chat_id, title in chats:
            label = f" ({title})" if title else ""
            print(f"  {chat_id}{label}")


async def subscribe(settings: Settings) -> None:
    settings.require("avito_client_id", "avito_client_secret")
    settings.require_webhook_secret()
    url = settings.public_webhook_url()
    async with httpx.AsyncClient(timeout=20) as http:
        avito = AvitoClient(
            settings.avito_client_id,
            settings.avito_client_secret,
            http,
        )
        try:
            result = await avito.subscribe(url)
        except AvitoError as exc:
            raise SystemExit(_hint(exc)) from exc
    print("Авито принял вебхук.")
    print(url)
    print(f"Ответ: {result}")
    print("Сервер python -m app должен уже слушать этот адрес, иначе уведомления некуда доставлять.")


def _hint(exc: AvitoError) -> str:
    text = str(exc)
    if exc.status in {401, 403} and "/token" in exc.path:
        return text + "\nПроверьте AVITO_CLIENT_ID и AVITO_CLIENT_SECRET."
    if exc.status == 403:
        return (
            text
            + "\nДоступ к методу закрыт. Для мессенджера нужен платный тариф "
            "с API и ключи из раздела «Для профессионалов» → API."
        )
    if exc.status == 402:
        return text + "\nАвито просит оплату доступа к этому методу."
    return text


async def run_mail(settings: Settings) -> None:
    settings.require(
        "imap_user",
        "imap_password",
        "telegram_bot_token",
        "telegram_chat_id",
    )
    host = settings.imap_host_resolved()
    log.info("Почта %s через %s, папка %s", settings.imap_user, host, settings.imap_folder)
    async with httpx.AsyncClient(timeout=20) as http:
        telegram = TelegramClient(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            http,
        )
        poller = MailPoller(
            settings,
            telegram,
            SeenStore(Path(settings.state_path)),
        )
        await poller.run()


async def mail_check(settings: Settings) -> None:
    settings.require("imap_user", "imap_password", "telegram_bot_token")
    host = settings.imap_host_resolved()
    try:
        letters = await asyncio.to_thread(fetch_letters, settings, set())
    except MailboxError as exc:
        raise SystemExit(mailbox_hint(exc)) from exc
    print(
        f"Почта подключена: {settings.imap_user} через {host}, папка {settings.imap_folder}."
    )
    if not letters:
        print(f"За последние {settings.mail_lookback_days} дн. писем от Авито нет.")
        print("В Авито включите уведомления о сообщениях на этот ящик.")
    else:
        print(f"Писем от Авито: {len(letters)}")
        matched = 0
        for letter in letters:
            notify = should_notify(letter, accept_all=settings.mail_accept_all)
            matched += int(notify)
            mark = "уведомление" if notify else "пропуск"
            print(f"  [{mark}] {letter.subject}")
        if matched == 0:
            print(
                "Ни одно письмо не похоже на переписку. "
                "Если нужная тема в списке, поставьте MAIL_ACCEPT_ALL=true."
            )
    async with httpx.AsyncClient(timeout=20) as http:
        telegram = TelegramClient(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            http,
        )
        if settings.telegram_chat_id:
            await telegram.send("Проверка: почта подключена, бот может писать в этот чат.")
            print("Telegram: тестовое сообщение отправлено.")
            return
        updates = await telegram.get_updates()
        chats = chats_from_updates(updates)
        if not chats:
            raise SystemExit(
                "TELEGRAM_CHAT_ID пуст. Напишите боту любое сообщение и снова запустите python -m app mail-check."
            )
        print("TELEGRAM_CHAT_ID не задан. Подставьте один из чатов:")
        for chat_id, title in chats:
            label = f" ({title})" if title else ""
            print(f"  {chat_id}{label}")


def _run(coro: object) -> None:
    try:
        asyncio.run(coro)  # type: ignore[arg-type]
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
