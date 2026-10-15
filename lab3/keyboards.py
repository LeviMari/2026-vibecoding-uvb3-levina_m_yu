"""Inline-клавиатуры."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from dates import format_ru_date


def meeting_tasks_keyboard(tasks: list[dict], meeting_id: str) -> InlineKeyboardMarkup:
    # В кнопки «Добавить все» и «Пропустить» зашиваем id встречи,
    # чтобы они касались только задач этой встречи.
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Добавить все", callback_data=f"mt:all:{meeting_id}")],
    ]
    for i, t in enumerate(tasks[:12], start=1):
        label = t.get("title") or "задача"
        if len(label) > 40:
            label = label[:37] + "…"
        rows.append(
            [InlineKeyboardButton(f"{i}. {label}", callback_data=f"mt:one:{t['id']}")]
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
    """Только кнопки «Готово»: компактно, по 4 в ряд, с номером задачи из списка.

    Кнопку «В работе» убрали: статус ни на что не влиял, а кнопок было вдвое больше.
    Отметить начатую задачу можно фразой: «начала писать отчёт».
    """
    if len(tasks) == 1:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("✓ Готово", callback_data=f"tk:done:{tasks[0]['id']}")]]
        )
    buttons = [
        InlineKeyboardButton(f"✓ {i}", callback_data=f"tk:done:{t['id']}")
        for i, t in enumerate(tasks[:40], start=1)
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    return InlineKeyboardMarkup(rows)

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


def settings_keyboard(enabled: bool, meeting_reminders: bool = True) -> InlineKeyboardMarkup:
    label = "Выключить уведомления" if enabled else "Включить уведомления"
    meet = (
        "Выключить напоминания после встреч"
        if meeting_reminders
        else "Включить напоминания после встреч"
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Сменить часовой пояс", callback_data="st:tz")],
            [InlineKeyboardButton(label, callback_data="st:toggle")],
            [InlineKeyboardButton(meet, callback_data="st:meetings")],
        ]
    )


def timezone_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    """Выбор часового пояса: по одному городу в строке, текущий отмечен галочкой."""
    from dates import TIMEZONE_CHOICES, tz_label

    rows = []
    for name, _title in TIMEZONE_CHOICES:
        mark = "✓ " if name == current else ""
        rows.append([InlineKeyboardButton(f"{mark}{tz_label(name)}", callback_data=f"tz:{name}")])
    return InlineKeyboardMarkup(rows)

def deadline_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data="dl:today"),
                InlineKeyboardButton("Завтра", callback_data="dl:tomorrow"),
            ],
            [
                InlineKeyboardButton("До пятницы", callback_data="dl:friday"),
                InlineKeyboardButton("Без срока", callback_data="dl:none"),
            ],
        ]
    )


def calendar_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Обновить ссылку", callback_data="cal:update")],
            [InlineKeyboardButton("🔌 Отключить", callback_data="cal:off")],
        ]
    )


def forgotten_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Ещё актуально", callback_data=f"fg:ok:{task_id}"),
                InlineKeyboardButton("Удалить", callback_data=f"fg:del:{task_id}"),
            ]
        ]
    )


# Тексты постоянных кнопок под полем ввода. Нажатие отправляет этот текст —
# handlers.handle_text узнаёт его и вызывает нужную команду.
MENU_TODAY = "Сегодня"
MENU_TASKS = "Задачи"
MENU_WEEK = "Неделя"
MENU_MEETINGS = "Встречи"
MENU_REVIEW = "Итоги недели"
MENU_HELP = "Помощь"
MENU_CALENDAR = "Календарь"
MENU_SHEET = "Таблица"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное меню: самые частые действия в три ряда, без ярких эмодзи.

    Кнопку «Календарь» добавили по отзыву: коллега не нашла, как подключить календарь.
    """
    return ReplyKeyboardMarkup(
        [
            [MENU_TODAY, MENU_TASKS, MENU_WEEK],
            [MENU_MEETINGS, MENU_REVIEW],
            [MENU_CALENDAR, MENU_SHEET, MENU_HELP],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Задача, вопрос о встрече или файл саммари",
    )


def survey_invite_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Пройти опрос — 1 минута", callback_data="sv:start")]]
    )


WOULD_USE_OPTIONS = [
    ("yes", "Да"),
    ("rather_yes", "Скорее да"),
    ("rather_no", "Скорее нет"),
    ("no", "Нет"),
]


def survey_would_use_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"sv:{key}") for key, label in WOULD_USE_OPTIONS]]
    )


PRICE_OPTIONS = [
    ("0", "0 ₽"),
    ("lt100", "до 100 ₽"),
    ("100_300", "100–300 ₽"),
    ("gt300", "300+ ₽"),
]


def survey_price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"sp:{key}") for key, label in PRICE_OPTIONS]]
    )


def onboarding_keyboard() -> InlineKeyboardMarkup:
    """Первые шаги после знакомства — чтобы не искать функции по /help."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📎 Разобрать пример саммари", callback_data="ob:sample")],
            [InlineKeyboardButton("📅 Подключить календарь", callback_data="ob:cal")],
            [InlineKeyboardButton("🕐 Сменить часовой пояс", callback_data="ob:tz")],
        ]
    )


def sheet_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Открыть таблицу", url=url)],
            [InlineKeyboardButton("🔄 Обновить сейчас", callback_data="sh:sync")],
            [
                InlineKeyboardButton("Другая таблица", callback_data="sh:new"),
                InlineKeyboardButton("Отключить", callback_data="sh:off"),
            ],
        ]
    )


def calendar_connect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📅 Подключить календарь", callback_data="ob:cal")]]
    )
