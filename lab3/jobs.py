"""Автоматические уведомления через JobQueue.

У каждого пользователя свой часовой пояс, поэтому вместо фиксированного расписания
бот раз в час проверяет, у кого сейчас нужный местный час (9:00, 15:00, 19:00).
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from telegram.ext import ContextTypes

import calendar_service
import llm
from dates import format_ru_date, get_tz, set_current_tz, today
from handlers import _group_open_tasks, _review_payload, current_week_plan, ping_markup
from keyboards import review_keyboard
from storage import now_iso, store

logger = logging.getLogger(__name__)


def _chat_id(user: dict) -> int:
    return int(user.get("chat_id") or user["user_id"])


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
    start = datetime.combine(d0, time(0, 0), tzinfo=get_tz())
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


async def after_meetings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Каждые 10 минут: встреча длительностью от 15 минут закончилась только что."""
    users = await store.all_users()
    for user in users:
        set_current_tz(user.get("timezone"))
        now = datetime.now(get_tz())
        window_start = now - timedelta(minutes=10)
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
                    f"Встреча «{event['title']}» закончилась. Пришли саммари, запись "
                    "или наговори голосовым, о чём договорились, — разберу задачи.",
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
                "5 коротких вопросов, около минуты — это поможет сделать его удобнее.",
                reply_markup=survey_invite_keyboard(),
            )
            await storage.mark_survey(c["user_id"], invited=True)
            sent += 1
        except Exception:
            logger.exception("survey invite failed for %s", c.get("user_id"))
    return sent


async def _morning_for(context: ContextTypes.DEFAULT_TYPE, user: dict) -> None:
    """9:00 по будням: в понедельник итоги и план (как /review), в остальные дни — сводка."""
    if today().weekday() == 0:
        await _send_monday_review(context, _chat_id(user), user)
    else:
        await _send_day_digest(context, _chat_id(user), user)


async def _deadline_ping_for(context: ContextTypes.DEFAULT_TYPE, user: dict) -> None:
    """15:00: пинг по открытым задачам со сроком сегодня."""
    d0 = today().isoformat()
    for task in user.get("tasks") or []:
        if task.get("status") != "done" and task.get("deadline") == d0:
            await context.bot.send_message(
                _chat_id(user),
                f"📌 Сегодня срок: {task['title']}\nУспеешь закрыть?",
                reply_markup=ping_markup(task["id"]),
            )


async def hourly_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в час: каждому пользователю — уведомления по ЕГО местному времени."""
    import storage

    users = await store.all_users()
    try:
        # Кого пора пригласить в опрос (начал пользоваться >= 6 часов назад, >= 3 действий).
        survey_ids = {
            int(c["user_id"])
            for c in await storage.survey_candidates(min_hours=6, min_actions=3)
        }
    except Exception:
        logger.exception("survey candidates failed")
        survey_ids = set()

    for user in users:
        set_current_tz(user.get("timezone"))
        local = datetime.now(get_tz())
        hour, weekday = local.hour, local.weekday()
        try:
            if hour == 19 and int(user["user_id"]) in survey_ids:
                await send_survey_invites(context.bot, [user])
            if not user.get("notifications_enabled", True):
                continue
            if hour == 9 and weekday < 5:
                await _morning_for(context, user)
            elif hour == 15:
                await _deadline_ping_for(context, user)
        except Exception:
            logger.exception("hourly notification failed for user %s", user.get("user_id"))
    set_current_tz(None)


def schedule(application) -> None:
    jq = application.job_queue
    if jq is None:
        logger.error("JobQueue недоступна — установи python-telegram-bot[job-queue]")
        return
    # Запуск ровно в начале каждого часа (плюс 5 секунд запаса).
    now = datetime.now(get_tz())
    first = (60 - now.minute) * 60 - now.second + 5
    jq.run_repeating(hourly_tick, interval=3600, first=first, name="hourly_tick")
    jq.run_repeating(after_meetings, interval=600, first=60, name="after_meeting")
    logger.info("Расписание уведомлений включено: по местному времени каждого пользователя")
