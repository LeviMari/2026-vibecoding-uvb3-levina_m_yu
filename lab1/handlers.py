"""Обработчики команд, текста, файлов и inline-кнопок."""

from __future__ import annotations

import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

import llm
from config import MAX_TXT_BYTES
from dates import (
    DAY_KEYS,
    DAY_TITLES,
    plan_days,
    format_ru_date,
    last_week_bounds,
    parse_date,
    this_week_end,
    today,
    week_bounds,
)
from keyboards import (
    clarify_keyboard,
    deadline_ping_keyboard,
    meeting_tasks_keyboard,
    meetings_keyboard,
    open_tasks_keyboard,
    review_keyboard,
    settings_keyboard,
    week_plan_keyboard,
)
from storage import PRIORITY_EMOJI, PRIORITY_RU, STATUS_RU, new_id, now_iso, store

logger = logging.getLogger(__name__)

WAITING_NAME = 1

HELP_TEXT = (
    "Я личный ассистент по задачам и встречам.\n\n"
    "📎 Пришли .txt с саммари встречи — разберу и предложу задачи.\n"
    "✍️ Напиши задачу обычным текстом, например: «подготовить отчёт для Ани до пятницы, срочно».\n"
    "🔄 Можно писать «отчёт сделала», «перенеси презентацию на среду».\n"
    "💬 Спрашивай про встречи: «что решили по бюджету?»\n\n"
    "/tasks — открытые задачи\n"
    "/done — что сделано за неделю\n"
    "/week — план на неделю\n"
    "/meetings — прошедшие встречи\n"
    "/review — итоги недели для руководителя\n"
    "/settings — уведомления\n"
    "/setname — как тебя зовут на встречах\n"
    "/help — эта подсказка"
)


async def _user(update: Update) -> dict:
    tg = update.effective_user
    chat = update.effective_chat
    return await store.get_user(tg.id, chat.id if chat else tg.id)


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


def _task_line(task: dict) -> str:
    emoji = PRIORITY_EMOJI.get(task.get("priority", "medium"), "🟡")
    deadline = format_ru_date(task.get("deadline"))
    status = STATUS_RU.get(task.get("status", "new"), task.get("status", ""))
    return f"{emoji} {task['title']} · {deadline} · {status}"


def _make_task(title: str, deadline: str | None, priority: str, source: str = "text") -> dict:
    d = parse_date(deadline)
    return {
        "id": new_id(),
        "title": (title or "Задача").strip()[:240],
        "deadline": d.isoformat() if d else None,
        "priority": _normalize_priority(priority),
        "status": "new",
        "created_at": now_iso(),
        "completed_at": None,
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = await _user(update)
    name = user.get("name") or ""
    if name:
        await update.message.reply_text(
            f"Снова привет, {name}! 👋\n\n{HELP_TEXT}"
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
    if not name or len(name) > 80:
        await update.message.reply_text("Напиши короткое имя — как тебя называют коллеги.")
        return WAITING_NAME
    user = await _user(update)
    user["name"] = name
    await store.save_user(user)
    await update.message.reply_text(
        f"Запомнила: {name}. Можно менять командой /setname.\n\n{HELP_TEXT}"
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
    await update.message.reply_text(HELP_TEXT)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Загрузка .txt саммари встречи."""
    user = await _user(update)
    doc = update.message.document
    filename = (doc.file_name or "").lower()
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

        extracted = await llm.extract_meeting(text, user.get("name", ""), today().isoformat())
    except llm.LLMError as exc:
        await status.edit_text(exc.user_message)
        return
    except Exception:
        logger.exception("Ошибка разбора встречи")
        await status.edit_text("Не получилось обработать файл. Попробуй ещё раз.")
        return

    meeting_date = parse_date(extracted.get("date")) or today()
    meeting = {
        "id": new_id(),
        "date": meeting_date.isoformat(),
        "title": (extracted.get("title") or "Встреча").strip()[:200],
        "participants": extracted.get("participants") or [],
        "summary": extracted.get("summary") or [],
        "decisions": extracted.get("decisions") or [],
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

    parts = [
        f"📅 {format_ru_date(meeting['date'])} — {meeting['title']}",
    ]
    if meeting["participants"]:
        parts.append("👥 " + ", ".join(str(p) for p in meeting["participants"][:12]))
    if meeting["summary"]:
        parts.append("📝 Кратко:\n" + "\n".join(f"• {s}" for s in meeting["summary"][:6]))
    if meeting["decisions"]:
        parts.append("✅ Решения:\n" + "\n".join(f"• {s}" for s in meeting["decisions"][:8]))

    if pending:
        parts.append("📌 Задачи для тебя:")
        parts.extend(f"{i+1}. {_task_line(t)}" for i, t in enumerate(pending))
        await status.edit_text(
            "\n\n".join(parts),
            reply_markup=meeting_tasks_keyboard(pending, meeting["id"]),
        )
    else:
        parts.append("Персональных задач в саммари не нашла.")
        await status.edit_text("\n\n".join(parts))


async def add_new_task_from_text(update: Update, user: dict, text: str) -> None:
    try:
        data = await llm.extract_task(text, today().isoformat())
    except llm.LLMError as exc:
        await update.message.reply_text(exc.user_message)
        return
    title = str(data.get("title") or text).strip()
    task = _make_task(title, data.get("deadline"), data.get("priority"))
    user.setdefault("tasks", []).append(task)
    await store.save_user(user)
    await update.message.reply_text(
        "Поняла так и сохранила:\n" + _task_line(task),
        reply_markup=open_tasks_keyboard([task]),
    )


async def apply_task_action(user: dict, task_id: str, action: str, new_deadline: str | None = None) -> str | None:
    """Меняет задачу. Возвращает текст результата или None, если задача не найдена."""
    task = next((t for t in user.get("tasks", []) if t["id"] == task_id), None)
    if not task:
        return None
    if action == "complete":
        task["status"] = "done"
        task["completed_at"] = now_iso()
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
    await update.message.reply_text(answer)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободный текст: LLM выбирает intent и вызывается нужный сценарий."""
    text = (update.message.text or "").strip()
    if not text:
        return
    user = await _user(update)
    has_open = any(t.get("status") != "done" for t in user.get("tasks", []))
    has_meetings = bool(user.get("meetings"))
    try:
        open_titles = [t["title"] for t in user.get("tasks", []) if t.get("status") != "done"]
        # Передаём классификатору сами задачи, иначе «с Лизой обсудила» он путает с вопросом о встрече.
        intent = await llm.classify_intent(text, has_open, has_meetings, open_titles)
    except llm.LLMError as exc:
        await update.message.reply_text(exc.user_message)
        return

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
        "overdue": "⚠️ Просрочено",
        "today": "📌 Сегодня",
        "week": "📅 Эта неделя",
        "later": "🗓 Позже",
    }
    # Сквозная нумерация: тот же номер стоит на кнопках под сообщением.
    num = 0
    for key, title in titles.items():
        if groups[key]:
            lines = []
            for t in groups[key]:
                num += 1
                lines.append(f"{num}. {_task_line(t)}")
            blocks.append(f"{title}\n" + "\n".join(lines))
    await update.message.reply_text(
        "\n\n".join(blocks),
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


def _format_week_plan(user: dict, plan: dict) -> str:
    tasks = {t["id"]: t for t in user.get("tasks", [])}
    lines = ["🗓 План на неделю"]
    comment = plan.get("comment")
    if comment:
        lines.append(str(comment))
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
    normalized = {}
    for k, v in days.items():
        nk = aliases.get(str(k).lower(), str(k).lower())
        normalized[nk] = v
    days = normalized
    available = plan_days()
    # Если модель всё же поставила задачи на прошедший день — переносим их на первый доступный.
    available_keys = {key for key, _ in available}
    for key in DAY_KEYS:
        if key not in available_keys and days.get(key):
            first = available[0][0]
            days[first] = list(days.get(first) or []) + list(days[key])
    for key, day_date in available:
        items = days.get(key) or []
        lines.append(f"\n{DAY_TITLES[key]}, {format_ru_date(day_date)}")
        if not items:
            lines.append("• свободно")
            continue
        for item in items[:6]:
            if isinstance(item, dict):
                tid = item.get("id")
                note = item.get("note") or ""
            else:
                tid, note = str(item), ""
            task = tasks.get(tid)
            title = task["title"] if task else str(tid)
            extra = f"\n   ↳ {note}" if note else ""
            dl = (
                f" ⏰ до {format_ru_date(task.get('deadline'))}"
                if task and task.get("deadline")
                else ""
            )
            lines.append(f"• {title}{dl}{extra}")
    return "\n".join(lines)


async def send_week_plan(update: Update, user: dict, rebuild: bool) -> None:
    open_tasks = [t for t in user.get("tasks", []) if t.get("status") != "done"]
    if not open_tasks:
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text("Нет открытых задач — план собирать не из чего.")
        return
    try:
        days = [(key, d.isoformat()) for key, d in plan_days()]
        plan = await llm.build_week_plan(open_tasks, today().isoformat(), today().weekday(), days)
    except llm.LLMError as exc:
        target = update.effective_message
        await target.reply_text(exc.user_message)
        return
    user["week_plan"] = plan
    await store.save_user(user)
    text = _format_week_plan(user, plan)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=week_plan_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=week_plan_keyboard())


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    await send_week_plan(update, user, rebuild=True)


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
    return {
        "last_week_done": done,
        "last_week_meetings": meetings,
        "open_tasks": open_tasks,
        "overdue": overdue,
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
    state = "включены" if enabled else "выключены"
    await update.message.reply_text(
        f"🔔 Уведомления сейчас {state}.\n"
        "Будни 9:00 — сводка (в понедельник вместо неё итоги недели).\n"
        "За день до дедлайна 18:00 — напоминание.\n"
        "В день дедлайна 15:00 — пинг, если задача ещё открыта.",
        reply_markup=settings_keyboard(enabled),
    )


def _meeting_source(data: str) -> str | None:
    """Из callback_data вида "mt:all:<id>" достаём source задач этой встречи."""
    parts = data.split(":")
    return f"meeting:{parts[2]}" if len(parts) >= 3 and parts[2] else None


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
        lines = "\n".join(_task_line(t) for t in pending)
        await query.edit_message_text("Добавила все задачи:\n" + lines)
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
        parts = [f"📅 {format_ru_date(meeting.get('date'))} — {meeting.get('title')}"]
        if meeting.get("participants"):
            parts.append("👥 " + ", ".join(str(p) for p in meeting["participants"]))
        if meeting.get("summary"):
            parts.append("📝 " + "\n".join(f"• {s}" for s in meeting["summary"]))
        if meeting.get("decisions"):
            parts.append("✅ " + "\n".join(f"• {s}" for s in meeting["decisions"]))
        await query.edit_message_text("\n\n".join(parts))
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
        enabled = user["notifications_enabled"]
        state = "включены" if enabled else "выключены"
        await query.edit_message_text(
            f"Уведомления теперь {state}.",
            reply_markup=settings_keyboard(enabled),
        )
        return


# Используется джобами: показать клавиатуру пинга в день дедлайна.
def ping_markup(task_id: str):
    return deadline_ping_keyboard(task_id)
