"""Автоматические уведомления через JobQueue (по TIMEZONE из .env)."""

from __future__ import annotations

import logging
from datetime import time, timedelta

from telegram.ext import ContextTypes

import llm
from config import TZ
from dates import format_ru_date, today
from handlers import _group_open_tasks, _review_payload, ping_markup
from keyboards import review_keyboard
from storage import store

logger = logging.getLogger(__name__)


def _chat_id(user: dict) -> int:
    return int(user.get("chat_id") or user["user_id"])


async def morning_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Будни 9:00: в понедельник — итоги и план, в остальные дни — сводка на день."""
    is_monday = today().weekday() == 0
    users = await store.all_users()
    for user in users:
        if not user.get("notifications_enabled", True):
            continue
        chat_id = _chat_id(user)
        try:
            if is_monday:
                # В понедельник — тот же формат, что у /review: итоги + планы.
                await _send_monday_review(context, chat_id, user)
            else:
                await _send_day_digest(context, chat_id, user)
        except Exception:
            logger.exception("morning job failed for user %s", user.get("user_id"))


async def _send_monday_review(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: dict) -> None:
    payload = _review_payload(user)
    try:
        text = await llm.build_review(style="normal", today=today().isoformat(), **payload)
        user["last_review"] = text
        await store.save_user(user)
        await context.bot.send_message(chat_id, text, reply_markup=review_keyboard())
    except llm.LLMError as exc:
        await context.bot.send_message(chat_id, exc.user_message)


async def _send_day_digest(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: dict) -> None:
    groups = _group_open_tasks(user.get("tasks") or [])
    plan_today: list[str] = []
    plan = user.get("week_plan") or {}
    day_key = ("mon", "tue", "wed", "thu", "fri")[today().weekday()] if today().weekday() < 5 else None
    tasks_by_id = {t["id"]: t for t in user.get("tasks", [])}
    if day_key:
        for item in (plan.get("days") or {}).get(day_key) or []:
            tid = item.get("id") if isinstance(item, dict) else str(item)
            task = tasks_by_id.get(tid)
            if task and task.get("status") != "done":
                plan_today.append(task["title"])

    overdue = [t["title"] for t in groups["overdue"]]
    today_tasks = [t["title"] for t in groups["today"]]
    if not overdue and not today_tasks and not plan_today:
        # Не спамим пустыми утрами.
        return
    try:
        text = await llm.daily_digest_text(
            name=user.get("name") or "",
            today_tasks=today_tasks,
            overdue=overdue,
            plan_today=plan_today,
        )
    except llm.LLMError:
        parts = ["Доброе утро! ☀️"]
        if overdue:
            parts.append("Просрочено:\n" + "\n".join(f"• {x}" for x in overdue))
        if today_tasks:
            parts.append("Дедлайн сегодня:\n" + "\n".join(f"• {x}" for x in today_tasks))
        if plan_today:
            parts.append("По плану на сегодня:\n" + "\n".join(f"• {x}" for x in plan_today))
        text = "\n\n".join(parts)
    await context.bot.send_message(chat_id, text)


async def eve_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """18:00: напоминание о задачах, у которых дедлайн завтра."""
    tomorrow = (today() + timedelta(days=1)).isoformat()
    users = await store.all_users()
    for user in users:
        if not user.get("notifications_enabled", True):
            continue
        due = [
            t
            for t in user.get("tasks") or []
            if t.get("status") != "done" and t.get("deadline") == tomorrow
        ]
        if not due:
            continue
        lines = "\n".join(f"• {t['title']}" for t in due)
        try:
            await context.bot.send_message(
                _chat_id(user),
                f"⏰ Завтра дедлайн ({format_ru_date(tomorrow)}):\n{lines}",
            )
        except Exception:
            logger.exception("eve reminder failed for %s", user.get("user_id"))


async def deadline_pings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """15:00: пинг по открытым задачам с дедлайном сегодня."""
    d0 = today().isoformat()
    users = await store.all_users()
    for user in users:
        if not user.get("notifications_enabled", True):
            continue
        due = [
            t
            for t in user.get("tasks") or []
            if t.get("status") != "done" and t.get("deadline") == d0
        ]
        for task in due:
            try:
                await context.bot.send_message(
                    _chat_id(user),
                    f"📌 Сегодня срок: {task['title']}\nУспеешь закрыть?",
                    reply_markup=ping_markup(task["id"]),
                )
            except Exception:
                logger.exception("deadline ping failed for %s", user.get("user_id"))


def schedule(application) -> None:
    jq = application.job_queue
    if jq is None:
        logger.error("JobQueue недоступна — установи python-telegram-bot[job-queue]")
        return
    # В python-telegram-bot 0 = воскресенье, поэтому будни Пн–Пт — это 1..5.
    jq.run_daily(morning_digest, time=time(9, 0, tzinfo=TZ), days=(1, 2, 3, 4, 5), name="morning")
    jq.run_daily(eve_reminders, time=time(18, 0, tzinfo=TZ), name="eve")
    jq.run_daily(deadline_pings, time=time(15, 0, tzinfo=TZ), name="ping")
    logger.info("Расписание уведомлений включено (%s)", TZ)
