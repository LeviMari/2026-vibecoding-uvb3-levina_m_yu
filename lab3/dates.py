"""Работа с датами в часовом поясе бота."""

from __future__ import annotations

import contextvars
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import TIMEZONE_NAME, TZ

# Часовой пояс пользователя, чьё сообщение (или уведомление) сейчас обрабатывается.
# Ставится один раз в начале обработки (bot.apply_user_timezone, jobs.hourly_tick),
# а все функции ниже — today(), календарь, план — берут время по нему.
_current_tz: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_tz", default=None)

# Пояса для выбора в /settings: (IANA-имя, подпись).
TIMEZONE_CHOICES = [
    ("Europe/Kaliningrad", "Калининград"),
    ("Europe/Moscow", "Москва, Минск"),
    ("Asia/Tbilisi", "Тбилиси, Ереван, Баку"),
    ("Europe/Samara", "Самара"),
    ("Asia/Yekaterinburg", "Екатеринбург, Ташкент"),
    ("Asia/Almaty", "Алматы"),
    ("Asia/Novosibirsk", "Новосибирск"),
    ("Asia/Vladivostok", "Владивосток"),
]


def valid_tz(name: str | None) -> bool:
    try:
        ZoneInfo(str(name))
        return bool(name)
    except Exception:
        return False


def set_current_tz(name: str | None) -> None:
    _current_tz.set(name if valid_tz(name) else None)


def get_tz_name() -> str:
    return _current_tz.get() or TIMEZONE_NAME


def get_tz() -> ZoneInfo:
    name = _current_tz.get()
    return ZoneInfo(name) if name else TZ


def tz_label(name: str | None = None) -> str:
    """«Москва, Минск (UTC+3)» — для настроек и приветствия."""
    name = name or get_tz_name()
    title = dict(TIMEZONE_CHOICES).get(name, name)
    offset = datetime.now(ZoneInfo(name)).utcoffset()
    hours = int(offset.total_seconds() // 3600) if offset else 0
    return f"{title} (UTC{hours:+d})"


def today() -> date:
    return datetime.now(get_tz()).date()


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


def this_or_next_friday(ref: date | None = None) -> date:
    """Ближайшая пятница, включая сегодня, если сегодня пятница."""
    ref = ref or today()
    delta = 4 - ref.weekday()
    if delta < 0:
        delta += 7
    return ref + timedelta(days=delta)


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
