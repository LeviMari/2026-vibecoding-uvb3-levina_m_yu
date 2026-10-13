"""Работа с датами в часовом поясе бота."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from config import TZ


def today() -> date:
    return datetime.now(TZ).date()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = str(value).strip()[:10]
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def week_bounds(ref: date | None = None) -> tuple[date, date]:
    """Текущая неделя: понедельник — воскресенье."""
    ref = ref or today()
    start = ref - timedelta(days=ref.weekday())
    return start, start + timedelta(days=6)


def last_week_bounds(ref: date | None = None) -> tuple[date, date]:
    start, _ = week_bounds(ref)
    prev_start = start - timedelta(days=7)
    return prev_start, prev_start + timedelta(days=6)


def this_week_end(ref: date | None = None) -> date:
    return week_bounds(ref)[1]


def format_ru_date(value: date | str | None) -> str:
    d = parse_date(value) if isinstance(value, str) else value
    if d is None:
        return "без срока"
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{d.day} {months[d.month - 1]}"


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri")
DAY_TITLES = {
    "mon": "Понедельник",
    "tue": "Вторник",
    "wed": "Среда",
    "thu": "Четверг",
    "fri": "Пятница",
}


def plan_days(ref: date | None = None) -> list[tuple[str, date]]:
    """Рабочие дни, которые ещё можно планировать: с сегодняшнего дня до пятницы.

    В выходные планируем следующую неделю. Прошедшие дни недели не предлагаем.
    """
    ref = ref or today()
    if ref.weekday() >= 5:
        ref = ref + timedelta(days=7 - ref.weekday())
    return [
        (DAY_KEYS[i], ref + timedelta(days=i - ref.weekday()))
        for i in range(ref.weekday(), 5)
    ]
