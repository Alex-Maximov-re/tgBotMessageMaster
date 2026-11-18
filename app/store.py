"""Память об уже отправленных сообщениях, чтобы Авито и опрос не дублировали уведомления."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("avito_notify.store")

_LIMIT = 5000


class SeenStore:
    def __init__(self, path: Path):
        self.path = path
        self._ids: list[str] = []
        self._known: set[str] = set()
        self._load()

    @property
    def empty(self) -> bool:
        return not self._ids

    def snapshot(self) -> set[str]:
        return set(self._known)

    def claim(self, message_id: str) -> bool:
        """Резервирует id. False, если это сообщение уже обрабатывали."""
        if message_id in self._known:
            return False
        self._known.add(message_id)
        self._ids.append(message_id)
        if len(self._ids) > _LIMIT:
            self._ids = self._ids[-_LIMIT:]
            self._known = set(self._ids)
        self._save()
        return True

    def release(self, message_id: str) -> None:
        """Снимает резерв, если уведомление не удалось отправить."""
        if message_id not in self._known:
            return
        self._known.remove(message_id)
        self._ids = [item for item in self._ids if item != message_id]
        self._save()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ids = data.get("ids") if isinstance(data, dict) else None
            if not isinstance(ids, list):
                raise ValueError("ids is not a list")
            self._ids = [str(item) for item in ids][-_LIMIT:]
            self._known = set(self._ids)
        except (OSError, json.JSONDecodeError, ValueError):
            log.warning("Не читается %s, начинаю с пустой истории", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"ids": self._ids}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)
