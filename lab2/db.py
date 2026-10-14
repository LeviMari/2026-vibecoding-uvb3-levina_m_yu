"""SQLite: схема, инициализация и разовая миграция из data.json."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

from config import DATA_FILE, DB_PATH, TZ

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    notifications_enabled INTEGER NOT NULL DEFAULT 1,
    meeting_reminders_enabled INTEGER NOT NULL DEFAULT 1,
    calendar_url TEXT,
    calendar_ics TEXT,
    last_review TEXT NOT NULL DEFAULT '',
    pending_deadline TEXT,
    awaiting TEXT NOT NULL DEFAULT '',
    pending_new_task TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    deadline TEXT,
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'new',
    source_meeting_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    date TEXT,
    title TEXT NOT NULL,
    participants TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '[]',
    decisions TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS pending_tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    deadline TEXT,
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'new',
    source_meeting_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS week_plans (
    user_id INTEGER PRIMARY KEY,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_notified (
    user_id INTEGER NOT NULL,
    event_uid TEXT NOT NULL,
    event_date TEXT NOT NULL,
    PRIMARY KEY (user_id, event_uid, event_date)
);

CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_meetings_user ON meetings(user_id);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_action ON events(action);
"""


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """Одно соединение на процесс: sqlite3 потокобезопасен с check_same_thread=False + lock."""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute("PRAGMA journal_mode = WAL")
    return _conn


def execute(sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
    """Параметризованный запрос. Строки в SQL не подставляем."""
    with _lock:
        cur = get_conn().execute(sql, params)
        get_conn().commit()
        return cur


def executemany(sql: str, seq: list[tuple]) -> None:
    with _lock:
        get_conn().executemany(sql, seq)
        get_conn().commit()


class transaction:
    """Несколько запросов одной транзакцией: либо все применятся, либо ни один.

    Пример:
        with transaction() as conn:
            conn.execute("DELETE ...", (...,))
            conn.executemany("INSERT ...", rows)
    """

    def __enter__(self) -> sqlite3.Connection:
        _lock.acquire()
        self._conn = get_conn()
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
                logger.error("Транзакция отменена: %s", exc_type.__name__)
        finally:
            _lock.release()


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    with _lock:
        cur = get_conn().execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    with _lock:
        cur = get_conn().execute(sql, params)
        return cur.fetchone()


def _source_meeting_id(source: Any) -> str | None:
    text = str(source or "")
    if text.startswith("meeting:"):
        mid = text.split(":", 1)[1].strip()
        return mid or None
    return None


def _migrate_from_json() -> None:
    """Если есть data.json, а таблица users пустая — переносим и переименовываем файл."""
    users_count = query_one("SELECT COUNT(*) AS n FROM users")
    if users_count and int(users_count["n"]) > 0:
        return
    if not os.path.exists(DATA_FILE):
        return

    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Не удалось прочитать data.json для миграции: %s", exc)
        return

    users = (payload or {}).get("users") or {}
    if not users:
        logger.info("data.json пустой, мигрировать нечего")
        return

    n_users = n_tasks = n_meetings = n_pending = 0
    created = now_iso()

    for raw in users.values():
        if not isinstance(raw, dict):
            continue
        try:
            user_id = int(raw.get("user_id"))
        except (TypeError, ValueError):
            continue
        chat_id = int(raw.get("chat_id") or user_id)
        execute(
            """
            INSERT OR IGNORE INTO users (
                user_id, chat_id, name, notifications_enabled,
                meeting_reminders_enabled, calendar_url, calendar_ics,
                last_review, pending_deadline, awaiting, pending_new_task, created_at
            ) VALUES (?, ?, ?, ?, 1, NULL, NULL, ?, ?, '', NULL, ?)
            """,
            (
                user_id,
                chat_id,
                str(raw.get("name") or ""),
                1 if raw.get("notifications_enabled", True) else 0,
                str(raw.get("last_review") or ""),
                raw.get("_pending_deadline"),
                created,
            ),
        )
        n_users += 1

        for t in raw.get("tasks") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            created_at = t.get("created_at") or created
            execute(
                """
                INSERT OR IGNORE INTO tasks (
                    id, user_id, title, deadline, priority, status,
                    source_meeting_id, created_at, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(t["id"]),
                    user_id,
                    str(t.get("title") or "Задача")[:240],
                    t.get("deadline"),
                    t.get("priority") or "medium",
                    t.get("status") or "new",
                    _source_meeting_id(t.get("source")),
                    created_at,
                    t.get("completed_at"),
                    t.get("updated_at") or created_at,
                ),
            )
            n_tasks += 1

        for m in raw.get("meetings") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            execute(
                """
                INSERT OR IGNORE INTO meetings (
                    id, user_id, date, title, participants, summary, decisions, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(m["id"]),
                    user_id,
                    m.get("date"),
                    str(m.get("title") or "Встреча")[:200],
                    json.dumps(m.get("participants") or [], ensure_ascii=False),
                    json.dumps(m.get("summary") or [], ensure_ascii=False),
                    json.dumps(m.get("decisions") or [], ensure_ascii=False),
                    created,
                ),
            )
            n_meetings += 1

        for t in raw.get("pending_tasks") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            created_at = t.get("created_at") or created
            execute(
                """
                INSERT OR IGNORE INTO pending_tasks (
                    id, user_id, title, deadline, priority, status,
                    source_meeting_id, created_at, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(t["id"]),
                    user_id,
                    str(t.get("title") or "Задача")[:240],
                    t.get("deadline"),
                    t.get("priority") or "medium",
                    t.get("status") or "new",
                    _source_meeting_id(t.get("source")),
                    created_at,
                    t.get("completed_at"),
                    t.get("updated_at") or created_at,
                ),
            )
            n_pending += 1

        plan = raw.get("week_plan")
        if plan:
            execute(
                "INSERT OR REPLACE INTO week_plans (user_id, plan, created_at) VALUES (?, ?, ?)",
                (user_id, json.dumps(plan, ensure_ascii=False), created),
            )

    migrated = DATA_FILE + ".migrated"
    try:
        os.replace(DATA_FILE, migrated)
    except OSError as exc:
        logger.error("Не удалось переименовать data.json: %s", exc)

    logger.info(
        "Миграция data.json → SQLite: пользователей %s, задач %s, встреч %s, предложенных задач %s",
        n_users,
        n_tasks,
        n_meetings,
        n_pending,
    )


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    """Простая миграция схемы: добавить колонку в уже существующую базу."""
    cols = {row["name"] for row in get_conn().execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        get_conn().execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        logger.info("Добавлена колонка %s.%s", table, column)


def init_db() -> None:
    """Создаёт таблицы и при необходимости переносит JSON."""
    with _lock:
        get_conn().executescript(SCHEMA)
        # Отпечаток файла саммари — чтобы не загружать одну встречу дважды.
        _add_column_if_missing("meetings", "source_hash", "TEXT")
        get_conn().commit()
    _migrate_from_json()
    logger.info("База данных готова")
