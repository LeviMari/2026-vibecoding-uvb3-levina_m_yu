"""Обработчики команд, текста, файлов и inline-кнопок."""

from __future__ import annotations

import hashlib
import html
import logging
from datetime import datetime, time, timedelta

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

import calendar_service
import llm
from config import ADMIN_ID, MAX_TXT_BYTES, TZ
from dates import (
    DAY_KEYS,
    DAY_TITLES,
    format_ru_date,
    last_week_bounds,
    parse_date,
    plan_days,
    this_or_next_friday,
    this_week_end,
    today,
    week_bounds,
)
from export import ExportError, build_tasks_xlsx, export_filename
from keyboards import (
    MENU_HELP,
    MENU_MEETINGS,
    MENU_REVIEW,
    MENU_TASKS,
    MENU_TODAY,
    MENU_WEEK,
    calendar_keyboard,
    main_menu_keyboard,
    clarify_keyboard,
    deadline_choice_keyboard,
    deadline_ping_keyboard,
    forgotten_keyboard,
    meeting_tasks_keyboard,
    meetings_keyboard,
    open_tasks_keyboard,
    review_keyboard,
    settings_keyboard,
    week_plan_keyboard,
)
from storage import PRIORITY_RU, STATUS_RU, new_id, now_iso, store

logger = logging.getLogger(__name__)

WAITING_NAME = 1

CALENDAR_HELP = (
    "📅 Подключаем календарь — бот будет видеть твои встречи.\n\n"
    "Google Календарь (лучший вариант — обновляется сам):\n"
    "1) Открой calendar.google.com в браузере на компьютере (в приложении на телефоне так не получится).\n"
    "2) ⚙️ справа вверху → «Настройки».\n"
    "3) В левой колонке пролистай вниз до раздела «Настройки моих календарей» "
    "(в рабочем аккаунте он ниже «Импорт и экспорт»).\n"
    "4) Нажми на свой календарь → в раскрывшемся списке выбери «Интеграция календаря».\n"
    "5) Скопируй «Секретный адрес в формате iCal» (ссылка заканчивается на .ics) и пришли сюда.\n\n"
    "Apple Календарь (iCloud): правый клик по календарю → «Поделиться календарём» → "
    "галочка «Общий календарь» → пришли ссылку webcal://…\n\n"
    "Нет поля «Секретный адрес»? В рабочих аккаунтах его иногда отключает администратор. "
    "Тогда: Настройки → «Импорт и экспорт» → «Экспорт», распакуй архив и пришли сюда файл .ics "
    "со своей почтой в названии. Файл — снимок на сегодня, поэтому обновляй его раз в неделю.\n\n"
    "🔒 Ссылка секретная: после проверки я удалю её из чата."
)

HELP_TEXT = (
    "Я личный ассистент по задачам и встречам.\n\n"
    "📎 Пришли .txt с саммари встречи — разберу и предложу задачи.\n"
    "✍️ Напиши задачу обычным текстом, например: «подготовить отчёт для Ани до пятницы, срочно».\n"
    "🔄 Можно писать «отчёт сделала», «перенеси презентацию на среду».\n"
    "💬 Спрашивай про встречи: «что решили по бюджету?»\n\n"
    "Частые действия — кнопками под полем ввода, все команды — в кнопке «Меню» слева.\n\n"
    "/tasks — открытые задачи\n"
    "/done — что сделано за неделю\n"
    "/today — встречи и задачи на сегодня\n"
    "/week — план на неделю\n"
    "/meetings — прошедшие встречи\n"
    "/review — итоги недели для руководителя\n"
    "/calendar — Google Календарь (iCal или .ics)\n"
    "/stats — личная статистика\n"
    "/export — выгрузка в Excel\n"
    "/feedback — отзыв\n"
    "/settings — уведомления\n"
    "/setname — как тебя зовут на встречах\n"
    "/help — эта подсказка"
)


async def _user(update: Update, *, clear_await: bool = True) -> dict:
    tg = update.effective_user
    chat = update.effective_chat
    user = await store.get_user(tg.id, chat.id if chat else tg.id)
    if clear_await and user.get("awaiting"):
        user["awaiting"] = ""
        user["pending_new_task"] = None
        await store.save_user(user)
    return user


def _normalize_priority(value: str | None) -> str:
    raw = (value or "medium").lower()
    mapping = {
        "high": "high",
        "высокий": "high",
        "срочно": "high",
        "medium": "medium",
        "средний": "medium",
        "low": "low",
        "низкий": "low",
    }
    return mapping.get(raw, "medium")


HTML = "HTML"  # parse_mode Telegram: жирный <b>, курсив <i>
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def esc(value) -> str:
    """Экранируем текст от пользователя и нейросети, чтобы не сломать HTML-разметку."""
    return html.escape(str(value or ""), quote=False)


def short_date(value) -> str:
    d = parse_date(value) if not hasattr(value, "day") else value
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}" if d else ""


def _days_word(n: int) -> str:
    """1 день, 2 дня, 5 дней."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def _task_meta(task: dict) -> str:
    """Спокойная строка-подпись: только то, что важно. «Новая» и средний приоритет не показываем."""
    parts = []
    if task.get("deadline"):
        parts.append(f"до {short_date(task['deadline'])}")
        dl = parse_date(task["deadline"])
        if dl and dl < today() and task.get("status") != "done":
            parts.append(f"просрочена на {_days_word((today() - dl).days)}")
    if task.get("priority") == "high":
        parts.append("срочно")
    if task.get("status") == "in_progress":
        parts.append("в работе")
    elif task.get("status") == "done":
        parts.append("выполнена")
    return " · ".join(parts)


def _task_line(task: dict, num: int | None = None) -> str:
    """Задача в HTML: название, под ним курсивом срок и пометки."""
    prefix = f"{num}. " if num else ""
    line = f"{prefix}{esc(task['title'])}"
    meta = _task_meta(task)
    if meta:
        line += f"\n<i>    {meta}</i>"
    return line


def _make_task(title: str, deadline: str | None, priority: str, source: str = "text") -> dict:
    d = parse_date(deadline)
    stamp = now_iso()
    return {
        "id": new_id(),
        "title": (title or "Задача").strip()[:240],
        "deadline": d.isoformat() if d else None,
        "priority": _normalize_priority(priority),
        "status": "new",
        "created_at": stamp,
        "completed_at": None,
        "updated_at": stamp,
        "source": source,
    }


def _group_open_tasks(tasks: list[dict]) -> dict[str, list[dict]]:
    d0 = today()
    week_end = this_week_end(d0)
    groups = {"overdue": [], "today": [], "week": [], "later": []}
    for t in tasks:
        if t.get("status") == "done":
            continue
        dl = parse_date(t.get("deadline"))
        if dl is None:
            groups["later"].append(t)
        elif dl < d0:
            groups["overdue"].append(t)
        elif dl == d0:
            groups["today"].append(t)
        elif dl <= week_end:
            groups["week"].append(t)
        else:
            groups["later"].append(t)

    def _prio_key(task: dict) -> int:
        return {"high": 0, "medium": 1, "low": 2}.get(task.get("priority"), 1)

    for key in groups:
        groups[key].sort(key=lambda x: (_prio_key(x), x.get("deadline") or "9999"))
    return groups


def _compact_task(t: dict) -> dict:
    return {
        "title": t.get("title"),
        "deadline": t.get("deadline"),
        "priority": t.get("priority"),
        "status": t.get("status"),
        "created_at": t.get("created_at"),
        "completed_at": t.get("completed_at"),
    }


def current_week_plan(user: dict) -> dict:
    """План недели, только если он построен на ЭТОЙ неделе — старый план не показываем."""
    plan = user.get("week_plan") or {}
    plan_at = parse_date((user.get("week_plan_at") or "")[:10])
    start, end = week_bounds()
    if plan and plan_at and start <= plan_at <= end:
        return plan
    return {}


def _has_calendar(user: dict) -> bool:
    return bool(user.get("calendar_url") or user.get("calendar_ics"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = await _user(update)
    await store.log_event(user["user_id"], "start")
    name = user.get("name") or ""
    if name:
        await update.message.reply_text(
            f"Снова привет, {name}! 👋\n\n{HELP_TEXT}",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "Привет! Я помогу с задачами и встречами. 👋\n\n"
        "Как тебя зовут на рабочих встречах? Так я найду твои поручения в саммари."
    )
    return WAITING_NAME


async def setname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _user(update)
    await update.message.reply_text("Как тебя зовут на встречах? Напиши имя.")
    return WAITING_NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if name in MENU_ACTIONS:
        await update.message.reply_text("Сначала напиши своё имя — как тебя называют на встречах.")
        return WAITING_NAME
    if not name or len(name) > 80:
        await update.message.reply_text("Напиши короткое имя — как тебя называют коллеги.")
        return WAITING_NAME
    user = await _user(update)
    user["name"] = name
    await store.save_user(user)
    await update.message.reply_text(
        f"Запомнила: {name}. Можно менять командой /setname.\n\n{HELP_TEXT}",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def cancel_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, имя можно задать позже через /setname.")
    return ConversationHandler.END


async def waiting_name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Сначала напиши имя — или /cancel, чтобы пропустить.")
    return WAITING_NAME


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _user(update)
    await update.message.reply_text(HELP_TEXT, reply_markup=main_menu_keyboard())


async def _save_calendar(update: Update, user: dict, url: str | None, ics_text: str | None) -> None:
    try:
        ics = await calendar_service.validate_source(url=url, ics_text=ics_text)
    except calendar_service.CalendarError as exc:
        await update.effective_message.reply_text(exc.user_message)
        return
    user["calendar_url"] = url
    user["calendar_ics"] = None if url else ics
    user["awaiting"] = ""
    await store.save_user(user)
    calendar_service.invalidate_cache(user["user_id"])
    await store.log_event(user["user_id"], "calendar_connect")
    if url and update.message:
        try:
            await update.message.delete()
            await update.effective_chat.send_message(
                "Календарь подключен. Ссылку удалила из чата — она секретная. "
                "Команды /today и /week теперь учитывают встречи."
            )
            return
        except Exception:
            await update.effective_chat.send_message(
                "Календарь подключен. Не смогла удалить сообщение со ссылкой — "
                "лучше сотри его вручную, ссылка секретная."
            )
            return
    await update.effective_message.reply_text(
        "Календарь из файла .ics сохранён. Команды /today и /week теперь учитывают встречи.\n\n"
        "⚠️ Файл — снимок на сегодня: новые и перенесённые встречи я не увижу. "
        "Присылай свежий файл раз в неделю — старый заменится. "
        "Если найдёшь «Секретный адрес в формате iCal», пришли его через /calendar — "
        "тогда календарь будет обновляться сам."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Загрузка .txt саммари встречи или файла .ics календаря."""
    user = await store.get_user(update.effective_user.id, update.effective_chat.id)
    doc = update.message.document
    filename = (doc.file_name or "").lower()

    if filename.endswith(".ics"):
        try:
            tg_file = await doc.get_file()
            raw = await tg_file.download_as_bytearray()
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            logger.exception("Ошибка чтения .ics")
            await update.message.reply_text("Не получилось прочитать файл .ics. Попробуй ещё раз.")
            return
        await _save_calendar(update, user, url=None, ics_text=text)
        return

    user = await _user(update)
    if not filename.endswith(".txt"):
        await update.message.reply_text("Нужен текстовый файл .txt — другие форматы не разбираю.")
        return
    if doc.file_size and doc.file_size > MAX_TXT_BYTES:
        await update.message.reply_text(
            f"Файл слишком большой ({doc.file_size} байт). Пришли саммари до {MAX_TXT_BYTES // 1024} КБ."
        )
        return
    if not user.get("name"):
        await update.message.reply_text(
            "Сначала скажи, как тебя зовут на встречах — команда /setname. "
            "Иначе я могу перепутать, чьи это задачи."
        )
        return

    status = await update.message.reply_text("Читаю саммари и достаю задачи…")
    try:
        tg_file = await doc.get_file()
        raw = await tg_file.download_as_bytearray()
        if len(raw) > MAX_TXT_BYTES:
            await status.edit_text("Файл оказался больше лимита. Сожми текст и пришли ещё раз.")
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="replace")
        if not text.strip():
            await status.edit_text("Файл пустой.")
            return

        # Отпечаток содержимого: тот же файл второй раз не разбираем и не дублируем встречу.
        source_hash = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
        duplicate = next(
            (m for m in user.get("meetings") or [] if m.get("source_hash") == source_hash), None
        )
        if duplicate:
            await status.edit_text(
                f"Это саммари уже загружено: {format_ru_date(duplicate.get('date'))} — "
                f"{duplicate.get('title')}. Повторно не добавляю, чтобы задачи не задвоились. "
                "Открыть встречу можно в /meetings."
            )
            return

        extracted = await llm.extract_meeting(text, user.get("name", ""), today().isoformat())
    except llm.LLMError as exc:
        await status.edit_text(exc.user_message)
        return
    except Exception:
        logger.exception("Ошибка разбора встречи")
        await status.edit_text("Не получилось обработать файл. Попробуй ещё раз.")
        return

    meeting_date = parse_date(extracted.get("date")) or today()
    title = (extracted.get("title") or "Встреча").strip()[:200]

    # Вторая проверка на дубль — для встреч, загруженных до появления отпечатков:
    # та же дата и та же тема значит, что это саммари уже есть.
    def _norm(value: str) -> str:
        return " ".join("".join(ch for ch in str(value).lower() if ch.isalnum() or ch.isspace()).split())

    same = next(
        (
            m
            for m in user.get("meetings") or []
            if m.get("date") == meeting_date.isoformat() and _norm(m.get("title")) == _norm(title)
        ),
        None,
    )
    if same:
        if not same.get("source_hash"):
            same["source_hash"] = source_hash  # в следующий раз узнаем файл сразу, без нейросети
            await store.save_user(user)
        await status.edit_text(
            f"Это саммари уже загружено: {format_ru_date(same.get('date'))} — {same.get('title')}. "
            "Повторно не добавляю, чтобы задачи не задвоились. Открыть встречу можно в /meetings."
        )
        return

    meeting = {
        "id": new_id(),
        "date": meeting_date.isoformat(),
        "title": title,
        "participants": extracted.get("participants") or [],
        "summary": extracted.get("summary") or [],
        "decisions": extracted.get("decisions") or [],
        "created_at": now_iso(),
        "source_hash": source_hash,
    }
    user.setdefault("meetings", []).append(meeting)

    pending = []
    for item in extracted.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        pending.append(
            _make_task(
                title,
                item.get("deadline"),
                item.get("priority"),
                source=f"meeting:{meeting['id']}",
            )
        )
    # Не затираем задачи из других встреч: каждая встреча хранит свои предложения
    # (у задачи есть source = "meeting:<id>"), поэтому кнопки старых сообщений тоже работают.
    user["pending_tasks"] = (user.get("pending_tasks") or []) + pending
    await store.save_user(user)
    await store.log_event(user["user_id"], "meeting_upload")

    parts = _meeting_blocks(meeting, summary_limit=6, decisions_limit=8)
    if pending:
        parts.append("<b>Твои задачи</b>\n" + "\n".join(_task_line(t, i + 1) for i, t in enumerate(pending)))
        await status.edit_text(
            "\n\n".join(parts),
            parse_mode=HTML,
            reply_markup=meeting_tasks_keyboard(pending, meeting["id"]),
        )
    else:
        parts.append("<i>Персональных задач в саммари не нашла.</i>")
        await status.edit_text("\n\n".join(parts), parse_mode=HTML)


def _meeting_blocks(meeting: dict, summary_limit: int = 20, decisions_limit: int = 20) -> list[str]:
    """Карточка встречи: заголовок жирным, участники курсивом, разделы с заголовками."""
    parts = [f"<b>{esc(format_ru_date(meeting.get('date')))} · {esc(meeting.get('title'))}</b>"]
    if meeting.get("participants"):
        parts[0] += "\n<i>" + esc(", ".join(str(p) for p in meeting["participants"][:12])) + "</i>"
    if meeting.get("summary"):
        parts.append("<b>Кратко</b>\n" + "\n".join(f"• {esc(x)}" for x in meeting["summary"][:summary_limit]))
    if meeting.get("decisions"):
        parts.append("<b>Решения</b>\n" + "\n".join(f"• {esc(x)}" for x in meeting["decisions"][:decisions_limit]))
    return parts


async def add_new_task_from_text(update: Update, user: dict, text: str) -> None:
    try:
        data = await llm.extract_task(text, today().isoformat())
    except llm.LLMError as exc:
        await update.message.reply_text(exc.user_message)
        return
    title = str(data.get("title") or text).strip()
    deadline = data.get("deadline")
    if isinstance(deadline, str) and deadline.lower() in {"null", "none", ""}:
        deadline = None
    task = _make_task(title, deadline, data.get("priority"))
    if not task.get("deadline"):
        user["awaiting"] = "deadline"
        user["pending_new_task"] = task
        await store.save_user(user)
        await update.message.reply_text(
            f"<b>{esc(task['title'])}</b>\nДо какого числа сделать?",
            parse_mode=HTML,
            reply_markup=deadline_choice_keyboard(),
        )
        return
    user.setdefault("tasks", []).append(task)
    await store.save_user(user)
    await store.log_event(user["user_id"], "task_add")
    await update.message.reply_text(
        "Записала:\n" + _task_line(task),
        parse_mode=HTML,
        reply_markup=open_tasks_keyboard([task]),
    )


async def apply_task_action(user: dict, task_id: str, action: str, new_deadline: str | None = None) -> str | None:
    """Меняет задачу. Возвращает текст результата или None, если задача не найдена."""
    task = next((t for t in user.get("tasks", []) if t["id"] == task_id), None)
    if not task:
        return None
    task["updated_at"] = now_iso()
    if action == "complete":
        task["status"] = "done"
        task["completed_at"] = now_iso()
        await store.log_event(user["user_id"], "task_done")
        return f"✅ Готово: {task['title']}"
    if action == "in_progress":
        if task["status"] != "done":
            task["status"] = "in_progress"
        else:
            task["status"] = "in_progress"
            task["completed_at"] = None
        return f"🔄 В работе: {task['title']}"
    if action == "delete":
        user["tasks"] = [t for t in user["tasks"] if t["id"] != task_id]
        return f"🗑 Удалила: {task['title']}"
    if action == "postpone":
        dl = parse_date(new_deadline) or (today() + timedelta(days=1))
        task["deadline"] = dl.isoformat()
        if task.get("status") == "done":
            task["status"] = "in_progress"
            task["completed_at"] = None
        return f"📅 Перенесла «{task['title']}» на {format_ru_date(dl)}"
    if action == "touch":
        return f"📌 Оставила в списке: {task['title']}"
    return None


async def update_task_from_text(
    update: Update, user: dict, text: str, fallback_to_question: bool = False
) -> None:
    open_tasks = [t for t in user.get("tasks", []) if t.get("status") != "done"]
    if not open_tasks:
        await update.message.reply_text("Открытых задач нет — сначала добавь новую.")
        return
    try:
        data = await llm.match_task_update(text, open_tasks, today().isoformat())
    except llm.LLMError as exc:
        await update.message.reply_text(exc.user_message)
        return

    action = data.get("action") or "complete"
    if action not in {"complete", "postpone", "delete", "in_progress"}:
        action = "complete"
    ids = [str(i) for i in (data.get("task_ids") or [])]
    known = {t["id"] for t in user.get("tasks", [])}
    ids = [i for i in ids if i in known]
    ambiguous = bool(data.get("ambiguous")) or len(ids) != 1
    new_deadline = data.get("new_deadline")

    if not ids:
        if fallback_to_question:
            # Ни одна задача не подошла — значит, это всё-таки разговор о встречах.
            await answer_meeting_q(update, user, text)
            return
        await update.message.reply_text(
            "Не поняла, о какой задаче речь. Уточни название или открой список: /tasks"
        )
        return
    if ambiguous:
        titles = {t["id"]: t["title"] for t in user.get("tasks", [])}
        await update.message.reply_text(
            "Уточни, какую задачу имеешь в виду:",
            reply_markup=clarify_keyboard(ids, titles, action),
        )
        # Дедлайн для postpone кладём в user, чтобы кнопка знала куда переносить.
        user["_pending_deadline"] = new_deadline
        await store.save_user(user)
        return

    msg = await apply_task_action(user, ids[0], action, new_deadline)
    user.pop("_pending_deadline", None)
    await store.save_user(user)
    await update.message.reply_text(msg or "Не нашла эту задачу.")


async def answer_meeting_q(update: Update, user: dict, text: str) -> None:
    meetings = user.get("meetings") or []
    if not meetings:
        await update.message.reply_text(
            "Истории встреч пока нет. Пришли .txt саммари — тогда смогу отвечать на такие вопросы."
        )
        return
    try:
        answer = await llm.answer_meeting_question(text, meetings, user.get("name", ""))
    except llm.LLMError as exc:
        await update.message.reply_text(exc.user_message)
        return
    await store.log_event(user["user_id"], "question")
    await update.message.reply_text(answer)


def _history_period(date_from: str | None, date_to: str | None) -> tuple[str, str]:
    d_to = parse_date(date_to) or today()
    d_from = parse_date(date_from) or (d_to - timedelta(days=14))
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from.isoformat(), d_to.isoformat()


async def send_history(update: Update, user: dict, date_from: str | None, date_to: str | None) -> None:
    start, end = _history_period(date_from, date_to)
    try:
        done = await store.history_done(user["user_id"], start, end)
        meetings = await store.history_meetings(user["user_id"], start, end)
    except Exception:
        logger.exception("Ошибка выборки истории")
        await update.message.reply_text("Не получилось достать историю. Попробуй ещё раз.")
        return
    await store.log_event(user["user_id"], "question")
    parts = [f"За период {format_ru_date(start)} — {format_ru_date(end)}:"]
    if done:
        parts.append("Закрытые задачи:")
        for t in done:
            when = format_ru_date((t.get("completed_at") or "")[:10])
            parts.append(f"• {t.get('title')} ({when})")
    else:
        parts.append("Закрытых задач за этот период нет.")
    if meetings:
        parts.append("Встречи:")
        for m in meetings:
            parts.append(f"• {format_ru_date(m.get('date'))}: {m.get('title')}")
    await update.message.reply_text("\n".join(parts))


async def _handle_awaiting(update: Update, user: dict, text: str) -> bool:
    """True, если сообщение уже обработано как шаг диалога (календарь / отзыв)."""
    awaiting = user.get("awaiting") or ""
    if awaiting == "calendar":
        if not text.strip().lower().startswith(("http://", "https://", "webcal://")):
            # Пользователь передумал и пишет что-то другое — выходим из режима календаря.
            user["awaiting"] = ""
            await store.save_user(user)
            await update.message.reply_text(
                "Это не похоже на ссылку на календарь — подключение отменила. "
                "Чтобы попробовать снова, нажми /calendar."
            )
            return False
        url = text.strip()
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        await _save_calendar(update, user, url=url, ics_text=None)
        return True
    if awaiting == "deadline" and user.get("pending_new_task"):
        # На вопрос «до какого числа?» ответили текстом, а не кнопкой: «к среде», «без срока».
        task = user["pending_new_task"]
        if any(w in text.lower() for w in ("без срока", "не знаю", "нет срока", "потом")):
            task["deadline"] = None
        else:
            try:
                data = await llm.extract_task(f"{task['title']} — срок: {text}", today().isoformat())
            except llm.LLMError as exc:
                await update.message.reply_text(exc.user_message)
                return True
            parsed = parse_date(data.get("deadline"))
            if not parsed:
                await update.message.reply_text(
                    "Не поняла срок. Выбери кнопкой или напиши дату, например «к среде» или «25 октября».",
                    reply_markup=deadline_choice_keyboard(),
                )
                return True
            task["deadline"] = parsed.isoformat()
        task["updated_at"] = now_iso()
        user.setdefault("tasks", []).append(task)
        user["pending_new_task"] = None
        user["awaiting"] = ""
        await store.save_user(user)
        await store.log_event(user["user_id"], "task_add")
        await update.message.reply_text(
            "Записала:\n" + _task_line(task),
            parse_mode=HTML,
            reply_markup=open_tasks_keyboard([task]),
        )
        return True
    if awaiting == "feedback":
        body = text.strip()
        if not body:
            await update.message.reply_text("Напиши отзыв одним сообщением.")
            return True
        try:
            await store.add_feedback(user["user_id"], body)
        except Exception:
            logger.exception("Ошибка записи отзыва")
            await update.message.reply_text("Не получилось сохранить отзыв. Попробуй ещё раз.")
            return True
        user["awaiting"] = ""
        await store.save_user(user)
        await store.log_event(user["user_id"], "feedback")
        await update.message.reply_text("Спасибо! Отзыв сохранила.")
        return True
    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободный текст: LLM выбирает intent и вызывается нужный сценарий."""
    text = (update.message.text or "").strip()
    if not text:
        return
    # Кнопка постоянного меню — сразу вызываем команду, без нейросети.
    action = MENU_ACTIONS.get(text)
    if action:
        await action(update, context)
        return
    user = await store.get_user(update.effective_user.id, update.effective_chat.id)
    if await _handle_awaiting(update, user, text):
        return

    has_open = any(t.get("status") != "done" for t in user.get("tasks", []))
    has_meetings = bool(user.get("meetings"))
    try:
        open_titles = [t["title"] for t in user.get("tasks", []) if t.get("status") != "done"]
        # Передаём классификатору сами задачи, иначе «с Лизой обсудила» он путает с вопросом о встрече.
        classified = await llm.classify_intent(
            text, has_open, has_meetings, open_titles, today=today().isoformat()
        )
    except llm.LLMError as exc:
        await update.message.reply_text(exc.user_message)
        return

    intent = classified.get("intent") if isinstance(classified, dict) else classified
    logger.info("intent=%s user=%s", intent, user["user_id"])
    if intent == "new_task":
        await add_new_task_from_text(update, user, text)
    elif intent == "update_task":
        await update_task_from_text(update, user, text)
    elif intent == "meeting_question":
        if has_open and "?" not in text:
            # Страховка: «с Лизой про отчётность обсудила» модель иногда принимает за вопрос.
            # Без вопросительного знака сначала пробуем найти подходящую задачу,
            # и только если её нет — отвечаем по истории встреч.
            await update_task_from_text(update, user, text, fallback_to_question=True)
        else:
            await answer_meeting_q(update, user, text)
    elif intent == "history":
        await send_history(update, user, classified.get("date_from"), classified.get("date_to"))
    elif intent == "week_plan":
        await send_week_plan(update, user, rebuild=True)
    elif intent == "week_review":
        await send_review(update, user, style="normal")
    else:
        await update.message.reply_text(
            "Не уверена, что с этим делать. Можно:\n"
            "• написать новую задачу\n"
            "• обновить статус существующей\n"
            "• спросить про встречу\n"
            "• открыть /help"
        )


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    groups = _group_open_tasks(user.get("tasks") or [])
    open_all = groups["overdue"] + groups["today"] + groups["week"] + groups["later"]
    if not open_all:
        await update.message.reply_text("Открытых задач нет. Напиши новую или пришли саммари встречи.")
        return
    blocks = []
    titles = {
        "overdue": "Просрочено",
        "today": "Сегодня",
        "week": "На этой неделе",
        "later": "Позже",
    }
    # Сквозная нумерация: тот же номер стоит на кнопках под сообщением.
    num = 0
    for key, title in titles.items():
        if groups[key]:
            lines = []
            for t in groups[key]:
                num += 1
                lines.append(_task_line(t, num))
            blocks.append(f"<b>{title}</b>\n" + "\n".join(lines))
    blocks.append("<i>Отметить выполненное — кнопкой с номером задачи.</i>")
    await update.message.reply_text(
        "\n\n".join(blocks),
        parse_mode=HTML,
        reply_markup=open_tasks_keyboard(open_all),
    )


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    start, end = week_bounds()
    done = []
    for t in user.get("tasks") or []:
        if t.get("status") != "done":
            continue
        completed = parse_date((t.get("completed_at") or "")[:10])
        if completed and start <= completed <= end:
            done.append(t)
    if not done:
        await update.message.reply_text("На этой неделе ещё ничего не отмечено как выполненное.")
        return
    lines = "\n".join(f"✅ {t['title']}" for t in done)
    await update.message.reply_text(f"Сделано на этой неделе ({format_ru_date(start)} — {format_ru_date(end)}):\n{lines}")


def _meetings_word(n: int) -> str:
    """1 встреча, 2 встречи, 5 встреч."""
    if n % 10 == 1 and n % 100 != 11:
        word = "встреча"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "встречи"
    else:
        word = "встреч"
    return f"{n} {word}"


def _format_week_plan(user: dict, plan: dict, occupancy: dict | None = None) -> str:
    """План недели в HTML: день жирным, занятость курсивом, задачи списком, подсказки курсивом."""
    tasks = {t["id"]: t for t in user.get("tasks", [])}
    lines = ["<b>План на неделю</b>"]
    comment = plan.get("comment")
    if comment:
        lines.append(f"<i>{esc(comment)}</i>")
    days = plan.get("days") or {}
    aliases = {
        "monday": "mon",
        "tuesday": "tue",
        "wednesday": "wed",
        "thursday": "thu",
        "friday": "fri",
        "пн": "mon",
        "вт": "tue",
        "ср": "wed",
        "чт": "thu",
        "пт": "fri",
    }
    days = {aliases.get(str(k).lower(), str(k).lower()): v for k, v in days.items()}
    available = plan_days()
    # Если модель всё же поставила задачи на прошедший день — переносим их на первый доступный.
    available_keys = {key for key, _ in available}
    for key in DAY_KEYS:
        if key not in available_keys and days.get(key):
            first = available[0][0]
            days[first] = list(days.get(first) or []) + list(days[key])
    for key, day_date in available:
        items = days.get(key) or []
        lines.append("")
        lines.append(f"<b>{DAY_TITLES[key]}, {format_ru_date(day_date)}</b>")
        occ = (occupancy or {}).get(day_date.isoformat()) if occupancy else None
        if occ:
            free = round(float(occ["free_hours"]))
            lines.append(f"<i>{_meetings_word(occ['count'])} · свободно около {free} ч</i>")
        if not items:
            lines.append("свободный день")
            continue
        for item in items[:6]:
            # Подсказки нейросети (note) не показываем: по одному названию задачи
            # она давала общие советы. Оставляем только факты: срок, срочность, просрочку.
            tid = item.get("id") if isinstance(item, dict) else str(item)
            task = tasks.get(tid)
            if not task:
                continue
            line = f"• {esc(task['title'])}"
            meta = _task_meta(task)
            if meta:
                line += f" <i>({meta})</i>"
            lines.append(line)
    return "\n".join(lines)


async def send_week_plan(update: Update, user: dict, rebuild: bool) -> None:
    open_tasks = [t for t in user.get("tasks", []) if t.get("status") != "done"]
    if not open_tasks:
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text("Нет открытых задач — план собирать не из чего.")
        return
    occupancy: dict = {}
    try:
        days = [(key, d.isoformat()) for key, d in plan_days()]
        occupancy = await calendar_service.day_occupancy(user, [d for _, d in plan_days()])
        plan = await llm.build_week_plan(
            open_tasks,
            today().isoformat(),
            today().weekday(),
            days,
            occupancy=occupancy or None,
        )
    except llm.LLMError as exc:
        target = update.effective_message
        await target.reply_text(exc.user_message)
        return
    user["week_plan"] = plan
    user["week_plan_at"] = now_iso()
    await store.save_user(user)
    await store.log_event(user["user_id"], "week")
    text = _format_week_plan(user, plan, occupancy)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=HTML, reply_markup=week_plan_keyboard())
    else:
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=week_plan_keyboard())


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    await send_week_plan(update, user, rebuild=True)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    d0 = today()
    parts = [f"<b>Сегодня, {format_ru_date(d0)}</b>"]

    if _has_calendar(user):
        start = datetime.combine(d0, time(0, 0), tzinfo=TZ)
        end = start + timedelta(days=1)
        try:
            events = await calendar_service.events_for_user(user, start, end)
        except calendar_service.CalendarError as exc:
            parts.append(f"<i>{esc(exc.user_message)}</i>")
            events = []
        day_events = [e for e in events if e["start"].date() == d0]
        if day_events:
            parts.append(
                "<b>Встречи</b>\n"
                + "\n".join(
                    f"{e['start']:%H:%M}–{e['end']:%H:%M}  {esc(e['title'])}" for e in day_events
                )
            )
        else:
            parts.append("<i>Встреч в календаре на сегодня нет.</i>")

    groups = _group_open_tasks(user.get("tasks") or [])
    if groups["overdue"]:
        parts.append("<b>Просрочено</b>\n" + "\n".join(_task_line(t) for t in groups["overdue"]))
    if groups["today"]:
        parts.append("<b>Срок сегодня</b>\n" + "\n".join(_task_line(t) for t in groups["today"]))

    plan_today: list[str] = []
    plan = current_week_plan(user)
    day_key = ("mon", "tue", "wed", "thu", "fri")[d0.weekday()] if d0.weekday() < 5 else None
    tasks_by_id = {t["id"]: t for t in user.get("tasks", [])}
    if day_key:
        for item in (plan.get("days") or {}).get(day_key) or []:
            tid = item.get("id") if isinstance(item, dict) else str(item)
            task = tasks_by_id.get(tid)
            if task and task.get("status") != "done":
                plan_today.append(task["title"])
    if plan_today:
        parts.append("<b>По плану</b>\n" + "\n".join(f"• {esc(x)}" for x in plan_today))
    elif not plan:
        parts.append("<i>Плана на неделю ещё нет — собери его командой /week.</i>")
    if len(parts) == 1:
        parts.append("<i>На сегодня ничего не запланировано.</i>")

    await update.message.reply_text("\n\n".join(parts), parse_mode=HTML)


async def meetings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    meetings = user.get("meetings") or []
    if not meetings:
        await update.message.reply_text("Сохранённых встреч пока нет. Пришли .txt саммари.")
        return
    await update.message.reply_text(
        "Прошедшие встречи — нажми, чтобы открыть содержание:",
        reply_markup=meetings_keyboard(meetings),
    )


def _review_payload(user: dict) -> dict:
    lw_start, lw_end = last_week_bounds()
    done, meetings, open_tasks, overdue = [], [], [], []
    d0 = today()
    for t in user.get("tasks") or []:
        compact = _compact_task(t)
        if t.get("status") == "done":
            completed = parse_date((t.get("completed_at") or "")[:10])
            if completed and lw_start <= completed <= lw_end:
                done.append(compact)
        else:
            open_tasks.append(compact)
            dl = parse_date(t.get("deadline"))
            if dl and dl < d0:
                overdue.append(compact)
    for m in user.get("meetings") or []:
        md = parse_date(m.get("date"))
        if md and lw_start <= md <= lw_end:
            meetings.append(
                {
                    "date": m.get("date"),
                    "title": m.get("title"),
                    "summary": m.get("summary"),
                    "decisions": m.get("decisions"),
                }
            )
    week_start, week_end = week_bounds()
    plan = None
    plan_at = parse_date((user.get("week_plan_at") or "")[:10])
    if user.get("week_plan") and plan_at and week_start <= plan_at <= week_end:
        plan = user.get("week_plan")
    return {
        "last_week_done": done,
        "last_week_meetings": meetings,
        "open_tasks": open_tasks,
        "overdue": overdue,
        "current_week_plan": plan,
    }


async def send_review(update: Update, user: dict, style: str) -> None:
    payload = _review_payload(user)
    try:
        text = await llm.build_review(style=style, today=today().isoformat(), **payload)
    except llm.LLMError as exc:
        await update.effective_message.reply_text(exc.user_message)
        return
    user["last_review"] = text
    await store.save_user(user)
    await store.log_event(user["user_id"], "review")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=review_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=review_keyboard())


async def review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    await send_review(update, user, style="normal")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    enabled = bool(user.get("notifications_enabled", True))
    meetings_on = bool(user.get("meeting_reminders_enabled", True))
    state = "включены" if enabled else "выключены"
    meet_state = "включены" if meetings_on else "выключены"
    await update.message.reply_text(
        f"🔔 Уведомления сейчас {state}.\n"
        f"Напоминания после встреч сейчас {meet_state}.\n"
        "Будни 9:00 — сводка (в понедельник вместо неё итоги недели).\n"
        "За день до дедлайна 18:00 — напоминание.\n"
        "В день дедлайна 15:00 — пинг, если задача ещё открыта.\n"
        "Пятница 17:00 — забытые задачи (открыты больше 14 дней).",
        reply_markup=settings_keyboard(enabled, meetings_on),
    )


async def calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await store.get_user(update.effective_user.id, update.effective_chat.id)
    if _has_calendar(user):
        via = "по iCal-ссылке" if user.get("calendar_url") else "из файла .ics"
        await update.message.reply_text(
            f"Календарь подключён ({via}). Ссылку в чат не повторяю — она секретная.",
            reply_markup=calendar_keyboard(),
        )
        return
    user["awaiting"] = "calendar"
    await store.save_user(user)
    await update.message.reply_text(CALENDAR_HELP)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    try:
        stats = await store.user_stats(user["user_id"])
    except Exception:
        logger.exception("Ошибка /stats")
        await update.message.reply_text("Не получилось посчитать статистику. Попробуй позже.")
        return
    lines = [
        "📊 Твоя статистика",
        f"Закрыто за эту неделю: {stats['done_week']}",
        f"Закрыто за 30 дней: {stats['done_30']}",
        f"Открыто сейчас: {stats['open']}",
        f"Просрочено: {stats['overdue']}",
        f"Встреч загружено за этот месяц: {stats['meetings_month']}",
    ]
    if stats["oldest"]:
        lines.append("Самые старые открытые:")
        for item in stats["oldest"]:
            lines.append(f"• {item['title']} — висит {item['days']} дн.")
    await update.message.reply_text("\n".join(lines))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    try:
        content = build_tasks_xlsx(user)
        filename = export_filename()
    except ExportError as exc:
        await update.message.reply_text(exc.user_message)
        return
    except Exception:
        logger.exception("Ошибка Excel")
        await update.message.reply_text("Не получилось собрать Excel. Попробуй ещё раз.")
        return
    from io import BytesIO

    await store.log_event(user["user_id"], "export")
    await update.message.reply_document(document=BytesIO(content), filename=filename)


async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await store.get_user(update.effective_user.id, update.effective_chat.id)
    user["awaiting"] = "feedback"
    await store.save_user(user)
    await update.message.reply_text("Напиши отзыв одним сообщением — что удобно, что мешает, чего не хватает.")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    if not ADMIN_ID or int(user["user_id"]) != int(ADMIN_ID):
        await update.message.reply_text("Эта команда только для администратора.")
        return
    try:
        data = await store.admin_overview()
    except Exception:
        logger.exception("Ошибка /admin")
        await update.message.reply_text("Не получилось собрать статистику.")
        return
    lines = [
        "🛠 Админка",
        f"Пользователей: {data['users']}",
        f"Активных за 7 дней: {data['active_7']}",
        f"Встреч всего: {data['meetings']}",
        f"Задач всего: {data['tasks']}",
        f"Вопросов всего: {data['questions']}",
        "Последние отзывы:",
    ]
    if not data["feedback"]:
        lines.append("• пока нет")
    for item in data["feedback"]:
        snippet = (item.get("text") or "").replace("\n", " ")[:180]
        lines.append(f"• {item.get('created_at')}: {snippet}")
    await update.message.reply_text("\n".join(lines))


def _meeting_source(data: str) -> str | None:
    """Из callback_data вида "mt:all:<id>" достаём source задач этой встречи."""
    parts = data.split(":")
    return f"meeting:{parts[2]}" if len(parts) >= 3 and parts[2] else None


def _settings_text(user: dict) -> str:
    enabled = bool(user.get("notifications_enabled", True))
    meetings_on = bool(user.get("meeting_reminders_enabled", True))
    state = "включены" if enabled else "выключены"
    meet_state = "включены" if meetings_on else "выключены"
    return (
        f"Уведомления теперь {state}.\n"
        f"Напоминания после встреч теперь {meet_state}."
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = await store.get_user(update.effective_user.id, update.effective_chat.id)

    if data.startswith("mt:all"):
        source = _meeting_source(data)
        all_pending = user.get("pending_tasks") or []
        pending = [t for t in all_pending if source is None or t.get("source") == source]
        if not pending:
            await query.edit_message_text("Нечего добавлять — задачи этой встречи уже разобраны.")
            return
        user.setdefault("tasks", []).extend(pending)
        added_ids = {t["id"] for t in pending}
        user["pending_tasks"] = [t for t in all_pending if t["id"] not in added_ids]
        await store.save_user(user)
        await store.log_event(user["user_id"], "task_add")
        lines = "\n".join(_task_line(t, i + 1) for i, t in enumerate(pending))
        await query.edit_message_text("<b>Добавила задачи</b>\n" + lines, parse_mode=HTML)
        return

    if data.startswith("mt:one:"):
        tid = data.split(":")[-1]
        pending = user.get("pending_tasks") or []
        task = next((t for t in pending if t["id"] == tid), None)
        if not task:
            # query.answer() уже вызван в начале функции, второй раз Telegram его не покажет,
            # поэтому отвечаем обычным сообщением.
            await query.message.reply_text("Эта задача уже добавлена или пропущена.")
            return
        user.setdefault("tasks", []).append(task)
        user["pending_tasks"] = [t for t in pending if t["id"] != tid]
        await store.save_user(user)
        await store.log_event(user["user_id"], "task_add")
        # Оставшиеся кнопки — только задачи той же встречи.
        rest = [t for t in user["pending_tasks"] if t.get("source") == task.get("source")]
        meeting_id = (task.get("source") or "").split(":", 1)[-1]
        note = f"Добавила: {task['title']}"
        if rest:
            await query.edit_message_text(
                note + "\n\nОстались:",
                reply_markup=meeting_tasks_keyboard(rest, meeting_id),
            )
        else:
            await query.edit_message_text(note + "\nДругих задач из встречи не осталось.")
        return

    if data.startswith("mt:skip"):
        source = _meeting_source(data)
        user["pending_tasks"] = [
            t for t in user.get("pending_tasks") or []
            if source is not None and t.get("source") != source
        ]
        await store.save_user(user)
        await query.edit_message_text("Ок, задачи из встречи не добавляла. Сама встреча уже в истории.")
        return

    if data.startswith("tk:done:"):
        tid = data.split(":")[-1]
        msg = await apply_task_action(user, tid, "complete")
        await store.save_user(user)
        await query.message.reply_text(msg or "Задача уже недоступна.")
        return

    if data.startswith("tk:work:"):
        tid = data.split(":")[-1]
        msg = await apply_task_action(user, tid, "in_progress")
        await store.save_user(user)
        await query.message.reply_text(msg or "Задача уже недоступна.")
        return

    if data.startswith("tk:tom:"):
        tid = data.split(":")[-1]
        tomorrow = (today() + timedelta(days=1)).isoformat()
        msg = await apply_task_action(user, tid, "postpone", tomorrow)
        await store.save_user(user)
        await query.message.reply_text(msg or "Задача уже недоступна.")
        return

    if data == "cl:cancel":
        await query.edit_message_text("Отменила.")
        return

    if data.startswith("cl:"):
        _, action, tid = data.split(":", 2)
        deadline = user.pop("_pending_deadline", None)
        msg = await apply_task_action(user, tid, action, deadline)
        await store.save_user(user)
        await query.edit_message_text(msg or "Задача уже недоступна.")
        return

    if data.startswith("mtg:"):
        mid = data.split(":", 1)[1]
        meeting = next((m for m in user.get("meetings", []) if m["id"] == mid), None)
        if not meeting:
            await query.edit_message_text("Эту встречу уже не нашла.")
            return
        await query.edit_message_text("\n\n".join(_meeting_blocks(meeting)), parse_mode=HTML)
        return

    if data == "plan:rebuild":
        await query.edit_message_text("Собираю план заново…")
        # После edit_message_text callback_query.message уже с другим текстом;
        # send_week_plan при callback снова сделает edit.
        fake_user = await store.get_user(user["user_id"])
        await send_week_plan(update, fake_user, rebuild=True)
        return

    if data in {"rv:short", "rv:long"}:
        style = "short" if data.endswith("short") else "long"
        await send_review(update, user, style=style)
        return

    if data == "st:toggle":
        user["notifications_enabled"] = not bool(user.get("notifications_enabled", True))
        await store.save_user(user)
        await query.edit_message_text(
            _settings_text(user),
            reply_markup=settings_keyboard(
                bool(user["notifications_enabled"]),
                bool(user.get("meeting_reminders_enabled", True)),
            ),
        )
        return

    if data == "st:meetings":
        user["meeting_reminders_enabled"] = not bool(user.get("meeting_reminders_enabled", True))
        await store.save_user(user)
        await query.edit_message_text(
            _settings_text(user),
            reply_markup=settings_keyboard(
                bool(user.get("notifications_enabled", True)),
                bool(user["meeting_reminders_enabled"]),
            ),
        )
        return

    if data.startswith("dl:"):
        task = user.get("pending_new_task")
        if not task:
            await query.edit_message_text("Задача уже сохранена или сброшена. Напиши её ещё раз.")
            return
        choice = data.split(":", 1)[1]
        if choice == "today":
            task["deadline"] = today().isoformat()
        elif choice == "tomorrow":
            task["deadline"] = (today() + timedelta(days=1)).isoformat()
        elif choice == "friday":
            task["deadline"] = this_or_next_friday().isoformat()
        else:
            task["deadline"] = None
        task["updated_at"] = now_iso()
        user.setdefault("tasks", []).append(task)
        user["pending_new_task"] = None
        user["awaiting"] = ""
        await store.save_user(user)
        await store.log_event(user["user_id"], "task_add")
        await query.edit_message_text(
            "Записала:\n" + _task_line(task),
            parse_mode=HTML,
            reply_markup=open_tasks_keyboard([task]),
        )
        return

    if data == "cal:update":
        user["awaiting"] = "calendar"
        await store.save_user(user)
        await query.edit_message_text(CALENDAR_HELP)
        return

    if data == "cal:off":
        user["calendar_url"] = None
        user["calendar_ics"] = None
        user["awaiting"] = ""
        await store.save_user(user)
        calendar_service.invalidate_cache(user["user_id"])
        await query.edit_message_text("Календарь отключила. /today и /week работают как раньше, без встреч.")
        return

    if data.startswith("fg:ok:"):
        tid = data.split(":")[-1]
        msg = await apply_task_action(user, tid, "touch")
        await store.save_user(user)
        await query.edit_message_text(msg or "Задача уже недоступна.")
        return

    if data.startswith("fg:del:"):
        tid = data.split(":")[-1]
        msg = await apply_task_action(user, tid, "delete")
        await store.save_user(user)
        await query.edit_message_text(msg or "Задача уже недоступна.")
        return


# Кнопки постоянного меню → команды. Заполняется в конце модуля, когда все функции объявлены.
MENU_ACTIONS: dict = {}


# Используется джобами: показать клавиатуру пинга в день дедлайна.
def ping_markup(task_id: str):
    return deadline_ping_keyboard(task_id)


def forgotten_markup(task_id: str):
    return forgotten_keyboard(task_id)


MENU_ACTIONS.update(
    {
        MENU_TODAY: today_cmd,
        MENU_TASKS: tasks_cmd,
        MENU_WEEK: week_cmd,
        MENU_MEETINGS: meetings_cmd,
        MENU_REVIEW: review_cmd,
        MENU_HELP: help_cmd,
    }
)
