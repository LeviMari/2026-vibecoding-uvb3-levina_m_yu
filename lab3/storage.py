"""Хранилище пользователей поверх SQLite.

Интерфейс get_user / save_user / all_users сохранён, чтобы handlers.py
работал с тем же словарём, что и раньше (tasks, meetings, pending_tasks).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from config import TZ
from db import execute, now_iso, query, query_one, transaction

logger = logging.getLogger(__name__)


def _empty_user(user_id: int, chat_id: int | None = None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "chat_id": chat_id or user_id,
        "name": "",
        "notifications_enabled": True,
        "meeting_reminders_enabled": True,
        "calendar_url": None,
        "calendar_ics": None,
        "tasks": [],
        "meetings": [],
        "pending_tasks": [],
        "last_review": "",
        "week_plan": None,
        "week_plan_at": None,
        "_pending_deadline": None,
        "awaiting": "",
        "pending_new_task": None,
        "created_at": now_iso(),
    }


def _json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _source_of(row: Any) -> str:
    mid = row["source_meeting_id"] if row["source_meeting_id"] else None
    return f"meeting:{mid}" if mid else "text"


def _meeting_id_of(task: dict) -> str | None:
    source = str(task.get("source") or "")
    if source.startswith("meeting:"):
        return source.split(":", 1)[1] or None
    return task.get("source_meeting_id")


def _task_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "deadline": row["deadline"],
        "priority": row["priority"],
        "status": row["status"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "updated_at": row["updated_at"],
        "source": _source_of(row),
        "source_meeting_id": row["source_meeting_id"],
    }


def _meeting_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "date": row["date"],
        "title": row["title"],
        "participants": _json_list(row["participants"]),
        "summary": _json_list(row["summary"]),
        "decisions": _json_list(row["decisions"]),
        "created_at": row["created_at"],
        "source_hash": row["source_hash"] if "source_hash" in row.keys() else None,
    }


def _user_from_db(user_id: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not row:
        return None
    tasks = [_task_from_row(t) for t in query("SELECT * FROM tasks WHERE user_id = ?", (user_id,))]
    meetings = [
        _meeting_from_row(m) for m in query("SELECT * FROM meetings WHERE user_id = ?", (user_id,))
    ]
    pending = [
        _task_from_row(t) for t in query("SELECT * FROM pending_tasks WHERE user_id = ?", (user_id,))
    ]
    plan_row = query_one("SELECT plan, created_at FROM week_plans WHERE user_id = ?", (user_id,))
    week_plan = None
    week_plan_at = None
    if plan_row:
        try:
            week_plan = json.loads(plan_row["plan"])
        except (json.JSONDecodeError, TypeError):
            week_plan = None
        week_plan_at = plan_row["created_at"]
    pending_task = None
    if row["pending_new_task"]:
        try:
            pending_task = json.loads(row["pending_new_task"])
        except (json.JSONDecodeError, TypeError):
            pending_task = None
    return {
        "user_id": row["user_id"],
        "chat_id": row["chat_id"],
        "name": row["name"] or "",
        "notifications_enabled": bool(row["notifications_enabled"]),
        "meeting_reminders_enabled": bool(row["meeting_reminders_enabled"]),
        "calendar_url": row["calendar_url"],
        "calendar_ics": row["calendar_ics"],
        "tasks": tasks,
        "meetings": meetings,
        "pending_tasks": pending,
        "last_review": row["last_review"] or "",
        "week_plan": week_plan,
        "week_plan_at": week_plan_at,
        "_pending_deadline": row["pending_deadline"],
        "awaiting": row["awaiting"] or "",
        "pending_new_task": pending_task,
        "created_at": row["created_at"],
        "timezone": row["timezone"] if "timezone" in row.keys() else None,
    }


def _insert_task_row(table: str, user_id: int, t: dict) -> tuple:
    created_at = t.get("created_at") or now_iso()
    return (
        str(t["id"]),
        user_id,
        str(t.get("title") or "Задача")[:240],
        t.get("deadline"),
        t.get("priority") or "medium",
        t.get("status") or "new",
        _meeting_id_of(t),
        created_at,
        t.get("completed_at"),
        t.get("updated_at") or created_at,
    )


class Storage:
    """Многопользовательское хранилище: ключ — Telegram user_id."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def get_user(self, user_id: int, chat_id: int | None = None) -> dict[str, Any]:
        async with self._lock:
            user = _user_from_db(int(user_id))
            if user is None:
                empty = _empty_user(int(user_id), chat_id)
                execute(
                    """
                    INSERT INTO users (
                        user_id, chat_id, name, notifications_enabled,
                        meeting_reminders_enabled, calendar_url, calendar_ics,
                        last_review, pending_deadline, awaiting, pending_new_task, created_at
                    ) VALUES (?, ?, '', 1, 1, NULL, NULL, '', NULL, '', NULL, ?)
                    """,
                    (int(user_id), int(chat_id or user_id), empty["created_at"]),
                )
                return empty
            if chat_id and int(user.get("chat_id") or 0) != int(chat_id):
                execute("UPDATE users SET chat_id = ? WHERE user_id = ?", (int(chat_id), int(user_id)))
                user["chat_id"] = int(chat_id)
            return deepcopy(user)

    async def save_user(self, user: dict[str, Any]) -> None:
        """Сохраняет пользователя целиком ОДНОЙ транзакцией.

        Раньше удаление и повторная вставка задач шли отдельными коммитами:
        при сбое посередине задачи пропали бы. Теперь либо всё, либо ничего.
        """
        async with self._lock:
            user_id = int(user["user_id"])
            pending_json = (
                json.dumps(user.get("pending_new_task"), ensure_ascii=False)
                if user.get("pending_new_task")
                else None
            )
            task_rows = [_insert_task_row("tasks", user_id, t) for t in user.get("tasks") or [] if t.get("id")]
            pending_rows = [
                _insert_task_row("pending", user_id, t)
                for t in user.get("pending_tasks") or []
                if t.get("id")
            ]
            meeting_rows = [
                (
                    str(m["id"]),
                    user_id,
                    m.get("date"),
                    str(m.get("title") or "Встреча")[:200],
                    json.dumps(m.get("participants") or [], ensure_ascii=False),
                    json.dumps(m.get("summary") or [], ensure_ascii=False),
                    json.dumps(m.get("decisions") or [], ensure_ascii=False),
                    m.get("created_at") or now_iso(),
                    m.get("source_hash"),
                )
                for m in user.get("meetings") or []
                if m.get("id")
            ]
            task_sql = """
                INSERT INTO {table} (
                    id, user_id, title, deadline, priority, status,
                    source_meeting_id, created_at, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            with transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, chat_id, name, notifications_enabled,
                        meeting_reminders_enabled, calendar_url, calendar_ics,
                        last_review, pending_deadline, awaiting, pending_new_task, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        chat_id = excluded.chat_id,
                        name = excluded.name,
                        notifications_enabled = excluded.notifications_enabled,
                        meeting_reminders_enabled = excluded.meeting_reminders_enabled,
                        calendar_url = excluded.calendar_url,
                        calendar_ics = excluded.calendar_ics,
                        last_review = excluded.last_review,
                        pending_deadline = excluded.pending_deadline,
                        awaiting = excluded.awaiting,
                        pending_new_task = excluded.pending_new_task
                    """,
                    (
                        user_id,
                        int(user.get("chat_id") or user_id),
                        str(user.get("name") or ""),
                        1 if user.get("notifications_enabled", True) else 0,
                        1 if user.get("meeting_reminders_enabled", True) else 0,
                        user.get("calendar_url"),
                        user.get("calendar_ics"),
                        str(user.get("last_review") or ""),
                        user.get("_pending_deadline"),
                        str(user.get("awaiting") or ""),
                        pending_json,
                        user.get("created_at") or now_iso(),
                    ),
                )
                # Имена таблиц фиксированные (не из ввода пользователя), значения — параметры.
                conn.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
                if task_rows:
                    conn.executemany(task_sql.format(table="tasks"), task_rows)
                conn.execute("DELETE FROM pending_tasks WHERE user_id = ?", (user_id,))
                if pending_rows:
                    conn.executemany(task_sql.format(table="pending_tasks"), pending_rows)
                conn.execute("DELETE FROM meetings WHERE user_id = ?", (user_id,))
                if meeting_rows:
                    conn.executemany(
                        """
                        INSERT INTO meetings (
                            id, user_id, date, title, participants, summary, decisions,
                            created_at, source_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        meeting_rows,
                    )
                plan = user.get("week_plan")
                if plan:
                    conn.execute(
                        "INSERT OR REPLACE INTO week_plans (user_id, plan, created_at) VALUES (?, ?, ?)",
                        (
                            user_id,
                            json.dumps(plan, ensure_ascii=False),
                            user.get("week_plan_at") or now_iso(),
                        ),
                    )
                else:
                    conn.execute("DELETE FROM week_plans WHERE user_id = ?", (user_id,))

    async def set_last_review(self, user_id: int, text: str) -> None:
        """Точечно обновить только текст итогов — чтобы рассылка не затёрла свежие изменения."""
        execute("UPDATE users SET last_review = ? WHERE user_id = ?", (text, int(user_id)))

    async def all_users(self) -> list[dict[str, Any]]:
        async with self._lock:
            rows = query("SELECT user_id FROM users")
            result = []
            for row in rows:
                user = _user_from_db(int(row["user_id"]))
                if user:
                    result.append(deepcopy(user))
            return result

    async def log_event(self, user_id: int, action: str) -> None:
        try:
            execute(
                "INSERT INTO events (user_id, action, created_at) VALUES (?, ?, ?)",
                (int(user_id), str(action), now_iso()),
            )
        except Exception:
            logger.exception("Не удалось записать событие %s", action)

    async def add_feedback(self, user_id: int, text: str) -> None:
        execute(
            "INSERT INTO feedback (user_id, text, created_at) VALUES (?, ?, ?)",
            (int(user_id), text[:4000], now_iso()),
        )

    async def user_stats(self, user_id: int) -> dict[str, Any]:
        """Личная статистика для /stats — выборки только параметризованные."""
        from datetime import timedelta

        from dates import today as _today

        today = _today()
        week_start = today - timedelta(days=today.weekday())
        month_ago = (today - timedelta(days=30)).isoformat()
        week_start_s = week_start.isoformat()
        today_s = today.isoformat()
        month_start = today.replace(day=1).isoformat()

        done_week = query_one(
            """
            SELECT COUNT(*) AS n FROM tasks
            WHERE user_id = ? AND status = 'done'
              AND substr(completed_at, 1, 10) >= ? AND substr(completed_at, 1, 10) <= ?
            """,
            (user_id, week_start_s, today_s),
        )
        done_30 = query_one(
            """
            SELECT COUNT(*) AS n FROM tasks
            WHERE user_id = ? AND status = 'done' AND substr(completed_at, 1, 10) >= ?
            """,
            (user_id, month_ago),
        )
        open_n = query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ? AND status != 'done'",
            (user_id,),
        )
        overdue = query_one(
            """
            SELECT COUNT(*) AS n FROM tasks
            WHERE user_id = ? AND status != 'done' AND deadline IS NOT NULL AND deadline < ?
            """,
            (user_id, today_s),
        )
        oldest = query(
            """
            SELECT title, created_at FROM tasks
            WHERE user_id = ? AND status != 'done'
            ORDER BY created_at ASC LIMIT 3
            """,
            (user_id,),
        )
        meetings_month = query_one(
            """
            SELECT COUNT(*) AS n FROM meetings
            WHERE user_id = ? AND substr(created_at, 1, 10) >= ?
            """,
            (user_id, month_start),
        )
        oldest_list = []
        for row in oldest:
            created = None
            try:
                created = datetime.fromisoformat(str(row["created_at"])[:19]).date()
            except ValueError:
                try:
                    created = datetime.fromisoformat(str(row["created_at"])[:10]).date()
                except ValueError:
                    created = today
            days = max(0, (today - created).days)
            oldest_list.append({"title": row["title"], "days": days})
        return {
            "done_week": int(done_week["n"] if done_week else 0),
            "done_30": int(done_30["n"] if done_30 else 0),
            "open": int(open_n["n"] if open_n else 0),
            "overdue": int(overdue["n"] if overdue else 0),
            "oldest": oldest_list,
            "meetings_month": int(meetings_month["n"] if meetings_month else 0),
        }

    async def history_done(self, user_id: int, date_from: str, date_to: str) -> list[dict[str, Any]]:
        rows = query(
            """
            SELECT title, deadline, completed_at, status FROM tasks
            WHERE user_id = ? AND status = 'done'
              AND substr(completed_at, 1, 10) >= ? AND substr(completed_at, 1, 10) <= ?
            ORDER BY completed_at ASC
            """,
            (user_id, date_from, date_to),
        )
        return [dict(r) for r in rows]

    async def history_meetings(self, user_id: int, date_from: str, date_to: str) -> list[dict[str, Any]]:
        rows = query(
            """
            SELECT date, title FROM meetings
            WHERE user_id = ?
              AND date IS NOT NULL AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (user_id, date_from, date_to),
        )
        return [dict(r) for r in rows]

    async def forgotten_open_tasks(self, user_id: int, older_than_iso: str) -> list[dict[str, Any]]:
        rows = query(
            """
            SELECT * FROM tasks
            WHERE user_id = ? AND status != 'done' AND updated_at <= ?
            """,
            (user_id, older_than_iso),
        )
        return [_task_from_row(r) for r in rows]

    async def mark_notified(self, user_id: int, event_uid: str, event_date: str) -> bool:
        """True, если запись новая (напоминание ещё не отправляли)."""
        existing = query_one(
            """
            SELECT 1 FROM calendar_notified
            WHERE user_id = ? AND event_uid = ? AND event_date = ?
            """,
            (user_id, event_uid, event_date),
        )
        if existing:
            return False
        execute(
            "INSERT OR IGNORE INTO calendar_notified (user_id, event_uid, event_date) VALUES (?, ?, ?)",
            (user_id, event_uid, event_date),
        )
        return True

    async def admin_overview(self) -> dict[str, Any]:
        from datetime import timedelta

        users_n = query_one("SELECT COUNT(*) AS n FROM users")
        week_ago = (datetime.now(TZ) - timedelta(days=7)).isoformat(timespec="seconds")
        active = query_one(
            "SELECT COUNT(DISTINCT user_id) AS n FROM events WHERE created_at >= ?",
            (week_ago,),
        )
        meetings_n = query_one("SELECT COUNT(*) AS n FROM meetings")
        tasks_n = query_one("SELECT COUNT(*) AS n FROM tasks")
        questions = query_one(
            "SELECT COUNT(*) AS n FROM events WHERE action = 'question'"
        )
        feedback_rows = query(
            "SELECT user_id, text, created_at FROM feedback ORDER BY id DESC LIMIT 5"
        )
        return {
            "users": int(users_n["n"] if users_n else 0),
            "active_7": int(active["n"] if active else 0),
            "meetings": int(meetings_n["n"] if meetings_n else 0),
            "tasks": int(tasks_n["n"] if tasks_n else 0),
            "questions": int(questions["n"] if questions else 0),
            "feedback": [dict(r) for r in feedback_rows],
        }


# ---------- Часовой пояс пользователя ----------

def get_user_tz(user_id: int) -> str | None:
    row = query_one("SELECT timezone FROM users WHERE user_id = ?", (int(user_id),))
    return row["timezone"] if row and row["timezone"] else None


async def set_user_tz(user_id: int, name: str) -> None:
    execute("UPDATE users SET timezone = ? WHERE user_id = ?", (name, int(user_id)))


# ---------- Опрос тестировщиков и статистика для Лабы 3 ----------

async def save_survey_answer(user_id: int, question: str, answer: str) -> None:
    execute(
        "INSERT INTO survey_answers (user_id, question, answer, created_at) VALUES (?, ?, ?, ?)",
        (int(user_id), question, answer[:4000], now_iso()),
    )


async def mark_survey(user_id: int, *, invited: bool = False, done: bool = False) -> None:
    if invited:
        execute("UPDATE users SET survey_invited_at = ? WHERE user_id = ?", (now_iso(), int(user_id)))
    if done:
        execute("UPDATE users SET survey_done_at = ? WHERE user_id = ?", (now_iso(), int(user_id)))


async def survey_candidates(
    min_hours: int = 6, min_actions: int = 3, include_invited: bool = False
) -> list[dict[str, Any]]:
    """Кого пригласить в опрос: начал пользоваться ботом >= min_hours часов назад,
    сделал >= min_actions действий и ещё не проходил опрос.
    include_invited=True — включить и тех, кого уже приглашали (ручная рассылка админа)."""
    from datetime import timedelta

    border = (datetime.now(TZ) - timedelta(hours=min_hours)).isoformat(timespec="seconds")
    rows = query(
        """
        SELECT u.user_id, u.chat_id, u.name
        FROM users u
        WHERE u.survey_done_at IS NULL
          AND (? OR u.survey_invited_at IS NULL)
          AND (SELECT MIN(created_at) FROM events e WHERE e.user_id = u.user_id) <= ?
          AND (SELECT COUNT(*) FROM events e WHERE e.user_id = u.user_id) >= ?
        """,
        (1 if include_invited else 0, border, min_actions),
    )
    return [dict(r) for r in rows]


async def survey_overview() -> dict[str, Any]:
    done = query_one("SELECT COUNT(*) AS n FROM users WHERE survey_done_at IS NOT NULL")
    would = query(
        "SELECT answer, COUNT(*) AS n FROM survey_answers WHERE question = 'would_use' GROUP BY answer"
    )
    price = query(
        "SELECT answer, COUNT(*) AS n FROM survey_answers WHERE question = 'price' GROUP BY answer"
    )
    return {
        "done": int(done["n"] if done else 0),
        "would_use": {r["answer"]: int(r["n"]) for r in would},
        "price": {r["answer"]: int(r["n"]) for r in price},
    }


async def actions_overview() -> list[dict[str, Any]]:
    rows = query("SELECT action, COUNT(*) AS n FROM events GROUP BY action ORDER BY n DESC")
    return [dict(r) for r in rows]


async def admin_export_data() -> dict[str, list[dict[str, Any]]]:
    """Всё для отчёта Лабы 3 одним набором: пользователи, опрос, отзывы, действия по дням."""
    users = query(
        """
        SELECT u.user_id, u.name, u.created_at,
               (SELECT COUNT(*) FROM events e WHERE e.user_id = u.user_id) AS actions,
               (SELECT MAX(created_at) FROM events e WHERE e.user_id = u.user_id) AS last_active,
               (SELECT COUNT(*) FROM meetings m WHERE m.user_id = u.user_id) AS meetings,
               (SELECT COUNT(*) FROM tasks t WHERE t.user_id = u.user_id) AS tasks,
               (u.calendar_url IS NOT NULL OR u.calendar_ics IS NOT NULL) AS has_calendar,
               u.survey_done_at
        FROM users u ORDER BY actions DESC
        """
    )
    survey = query(
        """
        SELECT s.created_at, u.name, s.question, s.answer
        FROM survey_answers s LEFT JOIN users u ON u.user_id = s.user_id
        ORDER BY s.user_id, s.id
        """
    )
    feedback = query(
        """
        SELECT f.created_at, u.name, f.text
        FROM feedback f LEFT JOIN users u ON u.user_id = f.user_id ORDER BY f.id
        """
    )
    daily = query(
        """
        SELECT substr(created_at, 1, 10) AS day, action, COUNT(*) AS n
        FROM events GROUP BY day, action ORDER BY day, action
        """
    )
    return {
        "users": [dict(r) for r in users],
        "survey": [dict(r) for r in survey],
        "feedback": [dict(r) for r in feedback],
        "daily": [dict(r) for r in daily],
    }


def new_id() -> str:
    """Короткий id, чтобы уместиться в callback_data Telegram (лимит 64 байта)."""
    return uuid.uuid4().hex[:8]


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


PRIORITY_RU = {"high": "высокий", "medium": "средний", "low": "низкий"}
STATUS_RU = {"new": "новая", "in_progress": "в работе", "done": "выполнена"}
PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

store = Storage()
