"""Google Календарь по iCal-ссылке или файлу .ics.

Секретную ссылку в логи не пишем. Повторяющиеся встречи раскрывает
recurring-ical-events. Время всегда в TIMEZONE из .env.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
from icalendar import Calendar
import recurring_ical_events

from dates import get_tz, get_tz_name

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(minutes=15)
WORK_START = time(10, 0)
WORK_END = time(19, 0)
WORK_HOURS = 9.0

# user_id -> (expires_at, events)
_cache: dict[tuple[int, str], tuple[datetime, list[dict[str, Any]]]] = {}


class CalendarError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _to_local_dt(value: Any) -> datetime | None:
    """Переводит DTSTART/DTEND в локальный datetime. date без времени — None (весь день)."""
    if value is None:
        return None
    raw = value.dt if hasattr(value, "dt") else value
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=get_tz())
        return raw.astimezone(get_tz())
    return None


def parse_ics(ics_text: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Разобрать ICS: только встречи с началом и концом, без CANCELLED."""
    try:
        calendar = Calendar.from_ical(ics_text)
    except Exception as exc:
        logger.warning("Некорректный ICS: %s", type(exc).__name__)
        raise CalendarError("Не получилось разобрать календарь. Нужен файл .ics или рабочая iCal-ссылка.") from None

    try:
        occurrences = recurring_ical_events.of(calendar).between(start, end)
    except Exception as exc:
        logger.warning("Ошибка раскрытия повторов ICS: %s", type(exc).__name__)
        raise CalendarError("Календарь прочитался, но повторяющиеся встречи разобрать не удалось.") from None

    events: list[dict[str, Any]] = []
    for item in occurrences:
        status = str(item.get("STATUS") or "").upper()
        if status == "CANCELLED":
            continue
        start_dt = _to_local_dt(item.get("DTSTART"))
        end_dt = _to_local_dt(item.get("DTEND"))
        if start_dt is None or end_dt is None:
            continue
        if end_dt <= start_dt:
            continue
        uid = str(item.get("UID") or "")
        title = str(item.get("SUMMARY") or "Встреча").strip() or "Встреча"
        # Сколько приглашённых: у личных блоков («Обед», «Фокус») их нет,
        # у настоящих встреч — минимум 2 (ты и коллега).
        attendees = item.get("ATTENDEE")
        if attendees is None:
            attendees_count = 0
        elif isinstance(attendees, list):
            attendees_count = len(attendees)
        else:
            attendees_count = 1
        events.append(
            {
                "uid": uid,
                "title": title,
                "start": start_dt,
                "end": end_dt,
                "attendees": attendees_count,
            }
        )
    events.sort(key=lambda e: e["start"])
    return events


async def download_ics(url: str) -> str:
    """Скачать iCal. URL в лог и в текст ошибки не попадает."""
    if not url or not url.lower().startswith(("http://", "https://")):
        raise CalendarError("Нужна ссылка, которая начинается с https://")
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.TimeoutException:
        raise CalendarError("Календарь не ответил вовремя. Проверь ссылку и попробуй ещё раз.") from None
    except httpx.HTTPError:
        logger.warning("Ошибка сети при скачивании календаря")
        raise CalendarError("Не получилось скачать календарь. Проверь ссылку.") from None
    if response.status_code >= 400:
        logger.warning("Календарь HTTP %s", response.status_code)
        raise CalendarError("Ссылка недоступна. Проверь, что это «секретный адрес в формате iCal».")
    text = response.text or ""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise CalendarError("По ссылке пришёл не календарь. Нужен секретный адрес в формате iCal.")
    return text


async def validate_source(url: str | None = None, ics_text: str | None = None) -> str:
    """Проверяет ссылку или текст .ics: должен парситься. Возвращает ICS."""
    if url:
        ics_text = await download_ics(url)
    if not ics_text:
        raise CalendarError("Пустой календарь.")
    now = datetime.now(get_tz())
    parse_ics(ics_text, now - timedelta(days=1), now + timedelta(days=30))
    return ics_text


def invalidate_cache(user_id: int) -> None:
    for key in [k for k in _cache if k[0] == int(user_id)]:
        _cache.pop(key, None)


async def events_for_user(
    user: dict[str, Any],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """События пользователя. Без календаря — пустой список, без ошибки."""
    user_id = int(user["user_id"])
    url = user.get("calendar_url")
    stored_ics = user.get("calendar_ics")
    if not url and not stored_ics:
        return []

    now = datetime.now(get_tz())
    cache_key = (user_id, get_tz_name())
    cached = _cache.get(cache_key)
    if cached and cached[0] > now:
        return [e for e in cached[1] if e["start"] < end and e["end"] > start]

    try:
        ics_text = stored_ics
        if url:
            ics_text = await download_ics(url)
        if not ics_text:
            return []
        # Кэш на широкий диапазон, чтобы /today и джобы не качали календарь каждый раз.
        window_start = now - timedelta(days=7)
        window_end = now + timedelta(days=21)
        all_events = parse_ics(ics_text, window_start, window_end)
        _cache[cache_key] = (now + CACHE_TTL, all_events)
        return [e for e in all_events if e["start"] < end and e["end"] > start]
    except CalendarError:
        raise
    except Exception:
        logger.exception("Календарь: неожиданная ошибка для пользователя %s", user_id)
        raise CalendarError("Не получилось прочитать календарь. Попробуй /calendar и обновить ссылку.") from None


# Личные блоки, после которых не нужно просить саммари.
PERSONAL_WORDS = ("обед", "перерыв", "lunch", "break", "фокус", "focus", "personal", "личное")


def is_real_meeting(event: dict[str, Any]) -> bool:
    """Встреча с другими людьми, а не личный блок в календаре."""
    title = event.get("title", "").lower()
    if any(word in title for word in PERSONAL_WORDS):
        return False
    return int(event.get("attendees") or 0) >= 2


def format_event_line(event: dict[str, Any]) -> str:
    start = event["start"].strftime("%H:%M")
    end = event["end"].strftime("%H:%M")
    return f"{start}–{end} {event['title']}"


def _overlap_hours(event: dict[str, Any], day: date) -> float:
    window_start = datetime.combine(day, WORK_START, tzinfo=get_tz())
    window_end = datetime.combine(day, WORK_END, tzinfo=get_tz())
    a = max(event["start"], window_start)
    b = min(event["end"], window_end)
    if b <= a:
        return 0.0
    return (b - a).total_seconds() / 3600.0


def occupancy_for_days(
    events: list[dict[str, Any]], days: list[date]
) -> dict[str, dict[str, Any]]:
    """Для каждого дня: список встреч и свободные часы в окне 10:00–19:00."""
    result: dict[str, dict[str, Any]] = {}
    for day in days:
        day_events = [e for e in events if e["start"].date() == day]
        busy = sum(_overlap_hours(e, day) for e in day_events)
        free = max(0.0, round(WORK_HOURS - busy, 1))
        result[day.isoformat()] = {
            "meetings": [
                {
                    "title": e["title"],
                    "start": e["start"].strftime("%H:%M"),
                    "end": e["end"].strftime("%H:%M"),
                }
                for e in day_events
            ],
            "count": len(day_events),
            "free_hours": free,
        }
    return result


async def day_occupancy(user: dict[str, Any], days: list[date]) -> dict[str, dict[str, Any]]:
    if not user.get("calendar_url") and not user.get("calendar_ics"):
        return {}
    if not days:
        return {}
    start = datetime.combine(min(days), time(0, 0), tzinfo=get_tz())
    end = datetime.combine(max(days) + timedelta(days=1), time(0, 0), tzinfo=get_tz())
    try:
        events = await events_for_user(user, start, end)
    except CalendarError:
        logger.warning("Календарь недоступен для плана пользователя %s", user.get("user_id"))
        return {}
    return occupancy_for_days(events, days)
