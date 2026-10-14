"""Автоматические уведомления через JobQueue (по TIMEZONE из .env)."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from telegram.ext import ContextTypes

import calendar_service
import llm
from config import TZ
from dates import format_ru_date, today
from handlers import _group_open_tasks, _review_payload, current_week_plan, forgotten_markup, ping_markup
from keyboards import review_keyboard
from storage import now_iso, store

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
        # Обновляем только итоги: копия user могла устареть, пока работала нейросеть.
        await store.set_last_review(user["user_id"], text)
        await context.bot.send_message(chat_id, text, reply_markup=review_keyboard())
    except llm.LLMError as exc:
        await context.bot.send_message(chat_id, exc.user_message)
    # В понедельник итоги заменяют сводку, но встречи на день всё равно показываем.
    meetings_today = await _meetings_today_lines(user)
    if meetings_today:
        await context.bot.send_message(
            chat_id, "📅 Встречи сегодня:\n" + "\n".join(f"• {x}" for x in meetings_today)
        )


async def _meetings_today_lines(user: dict) -> list[str]:
    if not (user.get("calendar_url") or user.get("calendar_ics")):
        return []
    d0 = today()
    start = datetime.combine(d0, time(0, 0), tzinfo=TZ)
    end = start + timedelta(days=1)
    try:
        events = await calendar_service.events_for_user(user, start, end)
    except calendar_service.CalendarError:
        logger.warning("Утренняя сводка: календарь недоступен для %s", user.get("user_id"))
        return []
    return [calendar_service.format_event_line(e) for e in events if e["start"].date() == d0]


async def _send_day_digest(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: dict) -> None:
    groups = _group_open_tasks(user.get("tasks") or [])
    plan_today: list[str] = []
    plan = current_week_plan(user)
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
    meetings_today = await _meetings_today_lines(user)
    if not overdue and not today_tasks and not plan_today and not meetings_today:
        # Не спамим пустыми утрами.
        return
    try:
        text = await llm.daily_digest_text(
            name=user.get("name") or "",
            today_tasks=today_tasks,
            overdue=overdue,
            plan_today=plan_today,
            meetings_today=meetings_today,
        )
    except llm.LLMError:
        parts = ["Доброе утро! ☀️"]
        if overdue:
            parts.append("Просрочено:\n" + "\n".join(f"• {x}" for x in overdue))
        if meetings_today:
            parts.append("Встречи сегодня:\n" + "\n".join(f"• {x}" for x in meetings_today))
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


async def forgotten_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пятница 17:00: открытые задачи без изменений больше 14 дней."""
    cutoff = (datetime.now(TZ) - timedelta(days=14)).isoformat(timespec="seconds")
    users = await store.all_users()
    for user in users:
        if not user.get("notifications_enabled", True):
            continue
        try:
            stale = await store.forgotten_open_tasks(user["user_id"], cutoff)
        except Exception:
            logger.exception("forgotten query failed for %s", user.get("user_id"))
            continue
        # Не спамим: максимум 5 самых давних задач за раз.
        stale.sort(key=lambda t: t.get("updated_at") or "")
        if len(stale) > 5:
            try:
                await context.bot.send_message(
                    _chat_id(user),
                    f"🕰 Без изменений больше 14 дней висят {len(stale)} задач. Покажу 5 самых давних.",
                )
            except Exception:
                logger.exception("forgotten summary failed for %s", user.get("user_id"))
        for task in stale[:5]:
            try:
                await context.bot.send_message(
                    _chat_id(user),
                    f"🕰 Задача висит без изменений больше 14 дней:\n{task['title']}\nЕщё актуально?",
                    reply_markup=forgotten_markup(task["id"]),
                )
            except Exception:
                logger.exception("forgotten ping failed for %s", user.get("user_id"))


async def after_meetings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Каждые 10 минут: встреча длительностью от 15 минут закончилась только что."""
    now = datetime.now(TZ)
    window_start = now - timedelta(minutes=10)
    users = await store.all_users()
    for user in users:
        if not user.get("notifications_enabled", True):
            continue
        if not user.get("meeting_reminders_enabled", True):
            continue
        if not (user.get("calendar_url") or user.get("calendar_ics")):
            continue
        try:
            events = await calendar_service.events_for_user(
                user, now - timedelta(hours=6), now + timedelta(minutes=1)
            )
        except calendar_service.CalendarError:
            continue
        except Exception:
            logger.exception("after-meeting calendar failed for %s", user.get("user_id"))
            continue
        for event in events:
            duration = event["end"] - event["start"]
            if duration < timedelta(minutes=15):
                continue
            if not (window_start < event["end"] <= now):
                continue
            if not calendar_service.is_real_meeting(event):
                # «Обед» и другие личные блоки без участников — саммари не просим.
                continue
            uid = event.get("uid") or event["title"]
            event_date = event["start"].date().isoformat()
            try:
                fresh = await store.mark_notified(user["user_id"], uid, event_date)
            except Exception:
                logger.exception("calendar_notified failed")
                continue
            if not fresh:
                continue
            try:
                await context.bot.send_message(
                    _chat_id(user),
                    f"Встреча «{event['title']}» закончилась. Пришли саммари — разберу задачи.",
                )
            except Exception:
                logger.exception("after-meeting send failed for %s", user.get("user_id"))


async def send_survey_invites(bot, candidates: list[dict]) -> int:
    """Разослать приглашение в опрос. Возвращает, скольким отправили."""
    import storage
    from keyboards import survey_invite_keyboard

    sent = 0
    for c in candidates:
        try:
            await bot.send_message(
                int(c.get("chat_id") or c["user_id"]),
                "Поделишься впечатлениями о боте? "
                "4 вопроса, около минуты — это поможет сделать его удобнее.",
                reply_markup=survey_invite_keyboard(),
            )
            await storage.mark_survey(c["user_id"], invited=True)
            sent += 1
        except Exception:
            logger.exception("survey invite failed for %s", c.get("user_id"))
    return sent


async def survey_invites(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Каждый день в 19:00: пригласить тех, кто начал пользоваться ботом минимум 6 часов назад."""
    import storage

    try:
        candidates = await storage.survey_candidates(min_hours=6, min_actions=3)
    except Exception:
        logger.exception("survey candidates failed")
        return
    await send_survey_invites(context.bot, candidates)


def schedule(application) -> None:
    jq = application.job_queue
    if jq is None:
        logger.error("JobQueue недоступна — установи python-telegram-bot[job-queue]")
        return
    # В python-telegram-bot 0 = воскресенье, поэтому будни Пн–Пт — это 1..5.
    jq.run_daily(morning_digest, time=time(9, 0, tzinfo=TZ), days=(1, 2, 3, 4, 5), name="morning")
    jq.run_daily(eve_reminders, time=time(18, 0, tzinfo=TZ), name="eve")
    jq.run_daily(deadline_pings, time=time(15, 0, tzinfo=TZ), name="ping")
    jq.run_daily(forgotten_tasks, time=time(17, 0, tzinfo=TZ), days=(5,), name="forgotten")
    jq.run_repeating(after_meetings, interval=600, first=60, name="after_meeting")
    jq.run_daily(survey_invites, time=time(19, 0, tzinfo=TZ), name="survey_invites")
    logger.info("Расписание уведомлений включено (%s)", TZ)
