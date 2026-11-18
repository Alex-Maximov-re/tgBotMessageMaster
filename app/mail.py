"""Опрос почты: письма Авито о новых сообщениях уходят в Telegram."""

from __future__ import annotations

import asyncio
import base64
import html
import imaplib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable

from app.config import Settings
from app.store import SeenStore

log = logging.getLogger("avito_notify.mail")

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_POSITIVE = (
    "новое сообщение",
    "вам написал",
    "написал вам",
    "написала вам",
    "сообщение от",
    "вам сообщение",
    "вам ответил",
    "новый ответ",
    "ответ на ваше",
    "ответил вам",
    "ответила вам",
    "новое входящее",
    "у вас новое",
)
_FOOTERS = (
    "отписаться",
    "вы получили это письмо",
    "это письмо отправлено автоматически",
    "чтобы отключить уведомления",
    "управление уведомлениями",
)
_URL = re.compile(r"https?://[^\s<>\"']+")
_MAX_FETCH = 40


@dataclass(frozen=True)
class MailLetter:
    store_id: str
    subject: str
    sender: str
    text: str
    link: str | None
    received: datetime | None


class MailboxError(Exception):
    pass


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href.strip())
        if tag in {"p", "br", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def encode_imap_utf7(text: str) -> str:
    """Имя папки в модифицированном UTF-7, который ждёт IMAP."""
    if text.upper() == "INBOX":
        return "INBOX"
    output: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        raw = "".join(pending).encode("utf-16-be")
        token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        output.append("&" + token + "-")
        pending.clear()

    for char in text:
        if char == "&":
            flush()
            output.append("&-")
        elif 0x20 <= ord(char) <= 0x7E:
            flush()
            output.append(char)
        else:
            pending.append(char)
    flush()
    return "".join(output)


def parse_search_uids(data: list[Any] | None) -> list[str]:
    if not data or data[0] is None:
        return []
    blob = data[0]
    text = blob.decode("ascii", errors="ignore") if isinstance(blob, bytes) else str(blob)
    return [part for part in text.split() if part.isdigit()]


def is_avito_address(from_header: str) -> bool:
    _name, address = parseaddr(from_header)
    domain = address.rsplit("@", 1)[-1].lower().strip().rstrip(">")
    return domain in {"avito.ru", "avito.pro"} or domain.endswith(
        (".avito.ru", ".avito.pro")
    )


def is_message_notification(subject: str, text: str, *, accept_all: bool) -> bool:
    if accept_all:
        return True
    haystack = _fold(f"{subject}\n{text[:800]}")
    return any(token in haystack for token in _POSITIVE)


def pick_link(links: list[str]) -> str | None:
    avito_links: list[str] = []
    for link in links:
        cleaned = link.strip().rstrip(").,;")
        lowered = cleaned.lower()
        if "avito.ru" not in lowered and "avito.pro" not in lowered:
            continue
        if any(marker in lowered for marker in ("unsubscribe", "otpis", "email-unsubscribe")):
            continue
        avito_links.append(cleaned)
    for link in avito_links:
        lowered = link.lower()
        if "messenger" in lowered or "message" in lowered:
            return link
    return avito_links[0] if avito_links else None


def format_mail_notification(letter: MailLetter) -> str:
    lines = ["<b>Новое сообщение на Авито</b>"]
    if letter.subject:
        lines.append(html.escape(letter.subject))
    if letter.text:
        lines.append("")
        lines.append(html.escape(letter.text))
    link = letter.link or "https://www.avito.ru/profile/messenger"
    lines.append("")
    lines.append(f'<a href="{html.escape(link, quote=True)}">Открыть Авито</a>')
    text = "\n".join(lines)
    if len(text) > 4000:
        return text[:3990] + "…"
    return text


def parse_raw_email(store_id: str, raw: bytes) -> MailLetter:
    message = message_from_bytes(raw)
    plain, html_body = _bodies(message)
    text, links = _visible_text(plain, html_body)
    return MailLetter(
        store_id=store_id,
        subject=_header(message.get("Subject")),
        sender=_header(message.get("From")),
        text=text,
        link=pick_link(links),
        received=_parse_date(message.get("Date")),
    )


def fetch_letters(settings: Settings, known_ids: set[str] | None = None) -> list[MailLetter]:
    known = known_ids or set()
    host = settings.imap_host_resolved()
    imap: imaplib.IMAP4_SSL | None = None
    try:
        try:
            imap = imaplib.IMAP4_SSL(host, settings.imap_port, timeout=30)
        except OSError as exc:
            raise MailboxError(
                f"Не удалось подключиться к {host}:{settings.imap_port}: {exc}"
            ) from exc
        try:
            imap.login(settings.imap_user, settings.imap_password)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"Почта не пустила: {exc}") from exc
        folder = encode_imap_utf7(settings.imap_folder)
        status, data = imap.select(folder, readonly=True)
        if status != "OK":
            raise MailboxError(
                f"Папка {settings.imap_folder!r} не открылась: {_imap_blob(data)}"
            )
        uidvalidity = _response_text(imap, "UIDVALIDITY") or "0"
        since = _since_date(settings.mail_lookback_days)
        found: set[str] = set()
        for domain in ("avito.ru", "avito.pro"):
            try:
                status, data = imap.uid("SEARCH", "SINCE", since, "FROM", domain)
            except imaplib.IMAP4.error as exc:
                raise MailboxError(f"Поиск писем не удался: {exc}") from exc
            if status == "OK":
                found.update(parse_search_uids(data))
        letters: list[MailLetter] = []
        for uid in sorted(found, key=int)[-_MAX_FETCH:]:
            store_id = f"mail:{uidvalidity}:{uid}"
            if store_id in known:
                continue
            try:
                status, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            except imaplib.IMAP4.error as exc:
                log.warning("Не прочиталось письмо %s: %s", uid, exc)
                continue
            if status != "OK":
                continue
            raw = _fetch_body(fetched)
            if raw is None:
                continue
            letters.append(parse_raw_email(store_id, raw))
        return letters
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                try:
                    imap.shutdown()
                except Exception:
                    pass


def mailbox_hint(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("auth", "login", "credential", "парол", "не пустила")
    ):
        return (
            text
            + "\nВключите IMAP в настройках ящика и запишите в IMAP_PASSWORD пароль приложения. "
            "Обычный пароль от входа на сайт почтовый сервер часто не принимает."
        )
    if "getaddrinfo" in lowered or "name or service not known" in lowered or "nodename" in lowered:
        return text + "\nПроверьте IMAP_HOST."
    if "timed out" in lowered or "timeout" in lowered:
        return text + "\nСервер почты не ответил. Проверьте интернет, IMAP_HOST и IMAP_PORT."
    return text


class MailPoller:
    def __init__(
        self,
        settings: Settings,
        telegram: Any,
        store: SeenStore,
        fetch: Callable[[Settings, set[str]], list[MailLetter]] = fetch_letters,
    ):
        self.settings = settings
        self.telegram = telegram
        self.store = store
        self._fetch = fetch
        self._started_at = time.time()

    async def poll_once(self, *, seed_only: bool) -> tuple[int, int]:
        letters = await asyncio.to_thread(
            self._fetch, self.settings, self.store.snapshot()
        )
        notified = 0
        remembered = 0
        for letter in letters:
            if not should_notify(letter, accept_all=self.settings.mail_accept_all):
                self.store.claim(letter.store_id)
                log.info("Письмо Авито не про переписку, пропуск: %s", letter.subject)
                continue
            if seed_only and _is_old(letter, self._started_at):
                if self.store.claim(letter.store_id):
                    remembered += 1
                log.info("Письмо уже было в ящике, без уведомления: %s", letter.subject)
                continue
            if not self.store.claim(letter.store_id):
                continue
            try:
                await self.telegram.send(format_mail_notification(letter))
            except Exception:
                self.store.release(letter.store_id)
                log.exception("Не удалось отправить уведомление о письме %s", letter.subject)
                continue
            notified += 1
            log.info("Уведомление по письму: %s", letter.subject)
        if seed_only:
            log.info(
                "Первый проход почты: старых писем %s, отправлено %s",
                remembered,
                notified,
            )
        return notified, remembered

    async def run(self) -> None:
        interval = max(15, self.settings.mail_poll_seconds)
        log.info("Смотрю почту каждые %s с", interval)
        seed = self.store.empty and not self.settings.mail_notify_existing
        while True:
            try:
                await self.poll_once(seed_only=seed)
                seed = False
            except asyncio.CancelledError:
                raise
            except MailboxError as exc:
                log.error("%s", mailbox_hint(exc))
            except Exception:
                log.exception("Опрос почты не удался")
            await asyncio.sleep(interval)

def should_notify(letter: MailLetter, *, accept_all: bool) -> bool:
    if accept_all:
        return True
    return is_avito_address(letter.sender) and is_message_notification(
        letter.subject, letter.text, accept_all=False
    )


def _is_old(letter: MailLetter, started_at: float, skew_seconds: float = 120) -> bool:
    if letter.received is None:
        return True
    moment = letter.received
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp() < started_at - skew_seconds


def _fold(value: str) -> str:
    return value.lower().replace("ё", "е")


def _header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _bodies(message: Message) -> tuple[str, str]:
    plain: list[str] = []
    html_parts: list[str] = []
    parts: list[Message]
    if message.is_multipart():
        parts = [part for part in message.walk() if part is not message]
    else:
        parts = [message]
    for part in parts:
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "") == "attachment":
            continue
        content_type = part.get_content_type()
        decoded = _decode_part(part)
        if content_type == "text/plain":
            plain.append(decoded)
        elif content_type == "text/html":
            html_parts.append(decoded)
    return "\n".join(plain), "\n".join(html_parts)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _visible_text(plain: str, html_body: str) -> tuple[str, list[str]]:
    html_text, html_links = _html_to_text(html_body)
    links = html_links + _links_from_text(plain) + _links_from_text(html_text)
    if len(_tidy(plain)) >= 40:
        return _tidy(plain), links
    if html_text.strip():
        return _tidy(html_text), links
    return _tidy(plain), links


def _html_to_text(html_body: str) -> tuple[str, list[str]]:
    if not html_body.strip():
        return "", []
    parser = _HtmlText()
    parser.feed(html_body)
    parser.close()
    return "".join(parser.parts), parser.links


def _links_from_text(text: str) -> list[str]:
    return _URL.findall(text)


def _tidy(text: str) -> str:
    normalized = text.replace("\xa0", " ").replace("\u200b", "")
    lines = [" ".join(line.split()) for line in normalized.splitlines()]
    compact = "\n".join(line for line in lines if line)
    folded = _fold(compact)
    cut_at = len(compact)
    for marker in _FOOTERS:
        index = folded.find(marker)
        if index != -1:
            cut_at = min(cut_at, index)
    compact = compact[:cut_at].strip()
    if len(compact) > 1200:
        return compact[:1200].rstrip() + "…"
    return compact


def _since_date(days: int) -> str:
    moment = datetime.now().astimezone() - timedelta(days=min(30, max(1, days)))
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year}"


def _response_text(imap: imaplib.IMAP4, name: str) -> str:
    _status, data = imap.response(name)
    return _imap_blob(data)


def _imap_blob(data: Any) -> str:
    if not data or data[0] is None:
        return ""
    item = data[0]
    if isinstance(item, bytes):
        return item.decode("utf-8", errors="replace").strip()
    return str(item).strip()


def _fetch_body(data: Any) -> bytes | None:
    if not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None
