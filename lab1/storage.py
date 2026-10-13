"""Атомарное хранение данных пользователей в JSON.

Каждый Telegram user_id — отдельный профиль: имя, задачи, встречи, настройки.
Пишем через временный файл и os.replace, чтобы data.json не побился при сбое.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from config import DATA_FILE, TZ

logger = logging.getLogger(__name__)

# Копии по умолчанию, чтобы не шарить один dict между пользователями.
def _empty_user(user_id: int, chat_id: int | None = None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "chat_id": chat_id or user_id,
        "name": "",
        "notifications_enabled": True,
        "tasks": [],
        "meetings": [],
        "pending_tasks": [],
        "last_review": "",
        "week_plan": None,
    }


class Storage:
    """Многопользовательское хранилище: ключ — Telegram user_id."""

    def __init__(self, path: str = DATA_FILE) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"users": {}}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
            if "users" not in self._data:
                self._data = {"users": {}}
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Не удалось прочитать data.json: %s", exc)
            self._data = {"users": {}}

    def _dump_atomic(self) -> None:
        """Пишем во временный файл и заменяем целевой — так JSON не повредится."""
        directory = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix="data.", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _key(self, user_id: int) -> str:
        return str(user_id)

    async def get_user(self, user_id: int, chat_id: int | None = None) -> dict[str, Any]:
        async with self._lock:
            key = self._key(user_id)
            users = self._data.setdefault("users", {})
            if key not in users:
                users[key] = _empty_user(user_id, chat_id)
                self._dump_atomic()
            else:
                if chat_id and users[key].get("chat_id") != chat_id:
                    users[key]["chat_id"] = chat_id
                    self._dump_atomic()
            return deepcopy(users[key])

    async def save_user(self, user: dict[str, Any]) -> None:
        async with self._lock:
            key = self._key(int(user["user_id"]))
            self._data.setdefault("users", {})[key] = deepcopy(user)
            self._dump_atomic()

    async def all_users(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [deepcopy(u) for u in self._data.get("users", {}).values()]

    async def update_user(self, user_id: int, **fields: Any) -> dict[str, Any]:
        user = await self.get_user(user_id)
        user.update(fields)
        await self.save_user(user)
        return user


def new_id() -> str:
    """Короткий id, чтобы уместиться в callback_data Telegram (лимит 64 байта)."""
    return uuid.uuid4().hex[:8]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


PRIORITY_RU = {"high": "высокий", "medium": "средний", "low": "низкий"}
STATUS_RU = {"new": "новая", "in_progress": "в работе", "done": "выполнена"}
PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

store = Storage()
