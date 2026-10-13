"""Inline-клавиатуры."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from dates import format_ru_date
from storage import PRIORITY_EMOJI


def meeting_tasks_keyboard(tasks: list[dict], meeting_id: str) -> InlineKeyboardMarkup:
    # В кнопки «Добавить все» и «Пропустить» зашиваем id встречи,
    # чтобы они касались только задач этой встречи.
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Добавить все", callback_data=f"mt:all:{meeting_id}")],
    ]
    for t in tasks[:12]:
        label = t.get("title") or "задача"
        if len(label) > 40:
            label = label[:37] + "…"
        prio = PRIORITY_EMOJI.get(t.get("priority", "medium"), "🟡")
        rows.append(
            [InlineKeyboardButton(f"{prio} {label}", callback_data=f"mt:one:{t['id']}")]
        )
    rows.append([InlineKeyboardButton("⏭ Пропустить", callback_data=f"mt:skip:{meeting_id}")])
    return InlineKeyboardMarkup(rows)


def task_actions_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Готово", callback_data=f"tk:done:{task_id}"),
                InlineKeyboardButton("🔄 В работе", callback_data=f"tk:work:{task_id}"),
            ]
        ]
    )


def open_tasks_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    # Telegram делит строку кнопок поровну и обрезает длинный текст,
    # поэтому на кнопках только номер задачи — полное название есть в тексте сообщения.
    rows = []
    single = len(tasks) == 1
    for i, t in enumerate(tasks[:20], start=1):
        suffix = "" if single else f" {i}"
        rows.append(
            [
                InlineKeyboardButton(f"✅ Готово{suffix}", callback_data=f"tk:done:{t['id']}"),
                InlineKeyboardButton(f"🔄 В работе{suffix}", callback_data=f"tk:work:{t['id']}"),
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def clarify_keyboard(task_ids: list[str], titles: dict[str, str], action: str) -> InlineKeyboardMarkup:
    rows = []
    for tid in task_ids[:8]:
        title = titles.get(tid, tid)
        if len(title) > 40:
            title = title[:37] + "…"
        rows.append([InlineKeyboardButton(title, callback_data=f"cl:{action}:{tid}")])
    rows.append([InlineKeyboardButton("Отмена", callback_data="cl:cancel")])
    return InlineKeyboardMarkup(rows)


def meetings_keyboard(meetings: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    # Сортируем по дате встречи (новые сверху), а не по порядку загрузки файлов.
    ordered = sorted(meetings, key=lambda m: m.get("date") or "", reverse=True)
    for m in ordered[:20]:
        title = m.get("title") or "Встреча"
        date = format_ru_date(m.get("date")) if m.get("date") else ""
        label = f"{date} · {title}"
        if len(label) > 50:
            label = label[:47] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"mtg:{m['id']}")])
    return InlineKeyboardMarkup(rows)


def week_plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔁 Пересобрать план", callback_data="plan:rebuild")]]
    )


def review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✂️ Короче", callback_data="rv:short"),
                InlineKeyboardButton("📝 Подробнее", callback_data="rv:long"),
            ]
        ]
    )


def deadline_ping_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Готово", callback_data=f"tk:done:{task_id}"),
                InlineKeyboardButton(
                    "➡️ Перенести на завтра", callback_data=f"tk:tom:{task_id}"
                ),
            ]
        ]
    )


def settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    label = "🔕 Выключить уведомления" if enabled else "🔔 Включить уведомления"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data="st:toggle")]]
    )
