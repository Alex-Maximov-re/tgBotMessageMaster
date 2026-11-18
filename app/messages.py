"""Разбор событий мессенджера Авито и текст уведомления."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_IGNORE_EVENT_TYPES = {"read", "typing", "seen", "mark_read", "chat_read"}
_INCOMING = {"in", "incoming", "inbound"}
_OUTGOING = {"out", "outgoing", "outbound"}
_TYPE_LABELS = {
    "image": "[изображение]",
    "voice": "[голосовое сообщение]",
    "file": "[файл]",
    "video": "[видео]",
    "appcall": "[звонок]",
    "call": "[звонок]",
    "location": "[геопозиция]",
    "link": "[ссылка]",
    "item": "[объявление]",
    "system": "[системное сообщение]",
    "deleted": "[сообщение удалено]",
}


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    author_id: int | None
    account_user_id: int | None
    item_id: int | None
    message_type: str
    text: str
    created: Any
    direction: str | None


@dataclass(frozen=True)
class ChatDetails:
    title: str | None = None
    url: str | None = None
    price: str | None = None
    names: dict[int, str] = field(default_factory=dict)


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def message_text(content: Any, message_type: str) -> str:
    if isinstance(content, str) and content.strip():
        return content.strip()
    data = content if isinstance(content, dict) else {}
    text = data.get("text") or data.get("body") or ""
    if isinstance(text, str) and text.strip():
        return text.strip()
    link = data.get("link") or data.get("url")
    if isinstance(link, dict):
        nested = link.get("url") or link.get("text") or ""
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    elif isinstance(link, str) and link.strip():
        return link.strip()
    label = _TYPE_LABELS.get(message_type.lower())
    if label:
        return label
    if message_type:
        return f"[{message_type}]"
    return "[сообщение без текста]"


def _direction(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized in _INCOMING:
        return "in"
    if normalized in _OUTGOING:
        return "out"
    return normalized


def parse_webhook(body: Any) -> IncomingMessage | None:
    if not isinstance(body, dict):
        return None
    payload = body.get("payload")
    event_type = ""
    if isinstance(payload, dict):
        event_type = str(payload.get("type") or "").strip().lower()
        value = payload.get("value")
    else:
        value = body.get("value") if isinstance(body.get("value"), dict) else body
    if event_type in _IGNORE_EVENT_TYPES:
        return None
    if not isinstance(value, dict):
        return None
    message_id = value.get("id") or value.get("message_id")
    chat_id = value.get("chat_id")
    if message_id is None or message_id == "" or not chat_id:
        return None
    message_type = str(value.get("type") or value.get("message_type") or "")
    return IncomingMessage(
        message_id=str(message_id),
        chat_id=str(chat_id),
        author_id=as_int(value.get("author_id")),
        account_user_id=as_int(value.get("user_id")),
        item_id=as_int(value.get("item_id")),
        message_type=message_type,
        text=message_text(value.get("content"), message_type),
        created=value.get("created"),
        direction=_direction(value.get("direction")),
    )


def extract_chats(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("chats", "result", "resources"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def message_from_chat(chat: dict[str, Any]) -> IncomingMessage | None:
    last = chat.get("last_message")
    if not isinstance(last, dict):
        return None
    value = dict(last)
    value.setdefault("chat_id", chat.get("id"))
    if "item_id" not in value:
        item_id = _item_id(chat)
        if item_id is not None:
            value["item_id"] = item_id
    return parse_webhook({"payload": {"type": "message", "value": value}})


def parse_chat_list(payload: Any) -> list[tuple[IncomingMessage, dict[str, Any]]]:
    found: list[tuple[IncomingMessage, dict[str, Any]]] = []
    for chat in extract_chats(payload):
        if not isinstance(chat, dict):
            continue
        message = message_from_chat(chat)
        if message is not None:
            found.append((message, chat))
    return found


def unwrap_chat(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("chat")
    if isinstance(nested, dict) and "context" not in payload and "users" not in payload:
        return nested
    return payload


def is_outgoing(
    message: IncomingMessage,
    account_user_id: int | None,
    *,
    notify_own: bool,
) -> bool:
    if notify_own:
        return False
    if message.direction == "out":
        return True
    if message.direction == "in":
        return False
    owner = account_user_id or message.account_user_id
    return (
        owner is not None
        and message.author_id is not None
        and message.author_id == owner
    )


def details_from_chat(chat: dict[str, Any] | None) -> ChatDetails:
    if not isinstance(chat, dict):
        return ChatDetails()
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    title = value.get("title") or context.get("title")
    url = value.get("url") or context.get("url")
    price = value.get("price_string")
    if price is None:
        price = value.get("price")
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        price = f"{price:g} ₽"
    names: dict[int, str] = {}
    users = chat.get("users")
    if isinstance(users, list):
        for user in users:
            if not isinstance(user, dict):
                continue
            user_id = as_int(user.get("id"))
            name = user.get("name") or user.get("public_name")
            if user_id is not None and isinstance(name, str) and name.strip():
                names[user_id] = name.strip()
    return ChatDetails(
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        url=url.strip() if isinstance(url, str) and url.strip() else None,
        price=str(price).strip() if price not in (None, "") else None,
        names=names,
    )


def created_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return timestamp
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return created_timestamp(int(text))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def format_created(value: Any) -> str | None:
    timestamp = created_timestamp(value)
    if timestamp is not None:
        return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def is_historical(message: IncomingMessage, started_at: float, skew_seconds: float = 15) -> bool:
    """Сообщение старше запуска: его не нужно слать при первом опросе."""
    timestamp = created_timestamp(message.created)
    if timestamp is None:
        return True
    return timestamp < started_at - skew_seconds


def format_notification(message: IncomingMessage, chat: dict[str, Any] | None) -> str:
    details = details_from_chat(unwrap_chat(chat) if chat else None)
    lines = ["<b>Новое сообщение на Авито</b>"]
    if details.title:
        safe_title = html.escape(details.title)
        if details.url:
            safe_url = html.escape(details.url, quote=True)
            lines.append(f'<b>Объявление:</b> <a href="{safe_url}">{safe_title}</a>')
        else:
            lines.append(f"<b>Объявление:</b> {safe_title}")
    elif message.item_id is not None:
        lines.append(f"<b>Объявление:</b> {message.item_id}")
    if details.price:
        lines.append(f"<b>Цена:</b> {html.escape(details.price)}")
    author = details.names.get(message.author_id) if message.author_id is not None else None
    if author:
        lines.append(f"<b>От:</b> {html.escape(author)}")
    elif message.author_id is not None:
        lines.append(f"<b>От:</b> id {message.author_id}")
    created = format_created(message.created)
    if created:
        lines.append(f"<b>Когда:</b> {html.escape(created)}")
    lines.append("")
    lines.append(html.escape(message.text))
    messenger = f"https://www.avito.ru/profile/messenger/channel/{message.chat_id}"
    lines.append("")
    lines.append(f'<a href="{html.escape(messenger, quote=True)}">Открыть чат</a>')
    text = "\n".join(lines)
    if len(text) > 4000:
        return text[:3990] + "…"
    return text


def brief_event(body: Any) -> str:
    if not isinstance(body, dict):
        return "unrecognized"
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return "unrecognized"
    value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
    return f"type={payload.get('type')} chat={value.get('chat_id')} id={value.get('id')}"


def _item_id(chat: dict[str, Any]) -> int | None:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    value = context.get("value") if isinstance(context.get("value"), dict) else {}
    return as_int(value.get("id") or context.get("item_id"))
