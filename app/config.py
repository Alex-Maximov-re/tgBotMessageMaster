"""Настройки из окружения и файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_ENV_NAMES = {
    "avito_client_id": "AVITO_CLIENT_ID",
    "avito_client_secret": "AVITO_CLIENT_SECRET",
    "avito_user_id": "AVITO_USER_ID",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "webhook_secret": "WEBHOOK_SECRET",
    "public_base_url": "PUBLIC_BASE_URL",
    "imap_user": "IMAP_USER",
    "imap_password": "IMAP_PASSWORD",
}

_IMAP_BY_DOMAIN = {
    "yandex.ru": "imap.yandex.ru",
    "yandex.com": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "mail.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "internet.ru": "imap.mail.ru",
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
}


def load_env_file(path: Path | None = None) -> None:
    """Подхватывает .env, не перезаписывая уже заданные переменные."""
    env_path = path or Path(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} должно быть целым числом, сейчас: {raw}") from exc


@dataclass
class Settings:
    avito_client_id: str = ""
    avito_client_secret: str = ""
    avito_user_id: int | None = None
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_secret: str = ""
    public_base_url: str = ""
    subscribe_on_start: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    poll_interval_seconds: int = 0
    poll_notify_existing: bool = False
    notify_own_messages: bool = False
    state_path: str = "data/state.json"
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    mail_poll_seconds: int = 60
    mail_lookback_days: int = 3
    mail_notify_existing: bool = False
    mail_accept_all: bool = False

    @classmethod
    def load(cls) -> Settings:
        load_env_file()
        port = _int("PORT", 8080)
        poll = _int("POLL_INTERVAL_SECONDS", 0)
        mail_poll = _int("MAIL_POLL_SECONDS", 60) or 60
        lookback = _int("MAIL_LOOKBACK_DAYS", 3) or 3
        return cls(
            avito_client_id=os.environ.get("AVITO_CLIENT_ID", "").strip(),
            avito_client_secret=os.environ.get("AVITO_CLIENT_SECRET", "").strip(),
            avito_user_id=_int("AVITO_USER_ID"),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", "").strip(),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            subscribe_on_start=_flag("SUBSCRIBE_ON_START", True),
            host=os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port if port is not None else 8080,
            poll_interval_seconds=poll if poll is not None else 0,
            poll_notify_existing=_flag("POLL_NOTIFY_EXISTING", False),
            notify_own_messages=_flag("NOTIFY_OWN_MESSAGES", False),
            state_path=os.environ.get("STATE_PATH", "data/state.json").strip()
            or "data/state.json",
            imap_host=os.environ.get("IMAP_HOST", "").strip(),
            imap_port=_int("IMAP_PORT", 993) or 993,
            imap_user=os.environ.get("IMAP_USER", "").strip(),
            imap_password=os.environ.get("IMAP_PASSWORD", "").strip(),
            imap_folder=os.environ.get("IMAP_FOLDER", "INBOX").strip() or "INBOX",
            mail_poll_seconds=max(15, mail_poll),
            mail_lookback_days=min(30, max(1, lookback)),
            mail_notify_existing=_flag("MAIL_NOTIFY_EXISTING", False),
            mail_accept_all=_flag("MAIL_ACCEPT_ALL", False),
        )

    def imap_host_resolved(self) -> str:
        if self.imap_host:
            return self.imap_host
        domain = self.imap_user.rsplit("@", 1)[-1].lower()
        host = _IMAP_BY_DOMAIN.get(domain)
        if host:
            return host
        raise SystemExit(
            "Укажите IMAP_HOST. Для этого почтового домена адрес IMAP не подставлен автоматически."
        )

    def require(self, *fields: str) -> None:
        missing = [field for field in fields if not getattr(self, field)]
        if missing:
            names = ", ".join(_ENV_NAMES.get(field, field) for field in missing)
            raise SystemExit(f"Заполните в .env: {names}")

    def require_webhook_secret(self) -> None:
        self.require("webhook_secret")
        try:
            self.webhook_secret.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SystemExit(
                "WEBHOOK_SECRET должен состоять из латинских букв и цифр"
            ) from exc

    def public_webhook_url(self) -> str:
        if not self.public_base_url:
            raise SystemExit("Заполните PUBLIC_BASE_URL — публичный https:// адрес без пути")
        token = quote(self.webhook_secret, safe="")
        return f"{self.public_base_url}/avito/webhook?token={token}"
