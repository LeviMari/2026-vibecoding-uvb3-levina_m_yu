"""Клиент LLM через OpenAI-совместимый HTTP API.

Пакет openai не используем: только httpx и POST {LLM_BASE_URL}/chat/completions.
Ключ API в логи не попадает. Для структурных ответов просим JSON и парсим его
с одной повторной попыткой.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, TIMEZONE_NAME

logger = logging.getLogger(__name__)

# Обёртки вида ```json ... ```, которые часто добавляет модель.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


class LLMError(Exception):
    """Ошибка запроса к модели, которую можно показать пользователю."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_content(raw: str) -> Any:
    """Достаём JSON из ответа модели, игнорируя markdown-обёртки."""
    cleaned = _strip_fences(raw)
    # Иногда модель пишет пояснение до/после JSON — берём первый объект/массив.
    match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1)
    return json.loads(cleaned)


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> str:
    """POST {LLM_BASE_URL}/chat/completions. Токен в лог не пишем."""
    if not LLM_API_KEY:
        raise LLMError("Не задан LLM_API_KEY в файле .env — без нейросети я не справлюсь.")

    url = f"{LLM_BASE_URL}/chat/completions"
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            # Не все совместимые API принимают response_format — пробуем без него.
            if response.status_code >= 400 and json_mode:
                payload.pop("response_format", None)
                response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        logger.warning("LLM timeout")
        raise LLMError("Нейросеть не ответила вовремя. Попробуй ещё раз чуть позже.") from None
    except httpx.HTTPError as exc:
        logger.warning("LLM network error: %s", type(exc).__name__)
        raise LLMError("Не получилось связаться с нейросетью. Проверь интернет и LLM_BASE_URL.") from None

    if response.status_code == 429:
        logger.warning("LLM rate limit")
        raise LLMError("Слишком много запросов к нейросети. Подожди минутку и повтори.")
    if response.status_code >= 400:
        logger.warning("LLM HTTP %s", response.status_code)
        raise LLMError("Нейросеть сейчас недоступна. Попробуй позже.")

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError):
        logger.warning("LLM unexpected response shape")
        raise LLMError("Нейросеть вернула странный ответ. Попробуй ещё раз.") from None

    if not content or not str(content).strip():
        raise LLMError("Нейросеть вернула пустой ответ. Попробуй переформулировать.")
    content = str(content)
    if not json_mode:
        # Telegram без parse_mode показывает **жирный** звёздочками — убираем разметку.
        content = re.sub(r"\*\*(.+?)\*\*", r"\1", content, flags=re.S)
        content = re.sub(r"^#{1,6}\s+", "", content, flags=re.M)
    return content


async def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.1,
) -> Any:
    """Запрос структурированного JSON: при ошибке парсинга — одна повторная попытка."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    extra = (
        "\n\nОтветь строго валидным JSON без пояснений и без markdown-обёрток."
    )
    messages[0]["content"] = system + extra

    last_error: Exception | None = None
    raw = ""
    for attempt in range(2):
        try:
            raw = await chat(messages, temperature=temperature, json_mode=True)
            return parse_json_content(raw)
        except LLMError:
            raise
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            logger.info("JSON parse failed, attempt %s", attempt + 1)
            messages = [
                messages[0],
                {"role": "user", "content": user},
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "Предыдущий ответ не является валидным JSON. Верни только JSON.",
                },
            ]
    raise LLMError("Не получилось разобрать ответ нейросети. Попробуй ещё раз.") from last_error


async def classify_intent(
    text: str,
    has_open_tasks: bool,
    has_meetings: bool,
    open_task_titles: list[str] | None = None,
    today: str | None = None,
) -> dict[str, Any]:
    """Классификация свободного текста в одно из действий бота."""
    data = await chat_json(
        system=(
            "Ты классификатор сообщений личного ассистента по задачам и встречам. "
            "Верни JSON вида {\"intent\": \"...\", \"date_from\": \"YYYY-MM-DD или null\", "
            "\"date_to\": \"YYYY-MM-DD или null\"}. "
            "intent — одно из: new_task, update_task, meeting_question, week_plan, week_review, history, other. "
            "new_task — пользователь ставит новую задачу. "
            "update_task — отмечает выполнение, переносит срок, удаляет или меняет статус существующей задачи. "
            "meeting_question — вопрос о прошедших встречах, решениях, договорённостях "
            "(конкретный факт из саммари, не список «что я сделала за период»). "
            "week_plan — просит составить или показать план на неделю. "
            "week_review — просит итоги недели / отчёт руководителю. "
            "history — ретроспектива за период: что сделала, какие задачи закрыла, чем занималась "
            "за дни/недели/месяц (\"что я сделала за последние 2 недели?\", "
            "\"какие задачи закрыла в сентябре?\"). Для history обязательно заполни date_from и date_to. "
            "other — приветствия, болтовня, непонятное.\n\n"
            "Важные правила:\n"
            "- Утверждение в прошедшем времени о сделанном (\"сделала\", \"обсудила\", "
            "\"отправила\", \"готово\", \"закрыла\") про КОНКРЕТНУЮ задачу — это update_task, даже если в нём "
            "упоминаются люди или темы со встреч.\n"
            "- Если сообщение похоже на одну из открытых задач пользователя (список ниже) "
            "и говорит о её выполнении, переносе или удалении — это update_task.\n"
            "- meeting_question — только когда пользователь СПРАШИВАЕТ о содержании встреч (обычно есть вопрос: "
            "что, когда, почему, кто, какие) и это не обзор «что я сделала за период».\n"
            f"Сегодня {today or ''}, относительные периоды для history переводи в абсолютные даты.\n"
            "Примеры: \"отчёт для Ани сделала\" -> update_task; "
            "\"с Лизой про отчётность обсудила\" -> update_task; "
            "\"перенеси созвон с Марком на среду\" -> update_task; "
            "\"что решили с Лизой по отчётности?\" -> meeting_question; "
            "\"позвонить Лизе до пятницы\" -> new_task; "
            "\"что я сделала за последние 2 недели?\" -> history; "
            "\"какие задачи закрыла в сентябре?\" -> history."
        ),
        user=(
            f"Есть история встреч: {has_meetings}.\n"
            "Открытые задачи пользователя:\n"
            + ("\n".join(f"- {t}" for t in (open_task_titles or [])[:50]) or "(нет)")
            + f"\n\nСообщение: {text}"
        ),
    )
    intent = str(data.get("intent", "other")).strip()
    allowed = {
        "new_task",
        "update_task",
        "meeting_question",
        "week_plan",
        "week_review",
        "history",
        "other",
    }
    return {
        "intent": intent if intent in allowed else "other",
        "date_from": data.get("date_from"),
        "date_to": data.get("date_to"),
    }


async def extract_meeting(text: str, user_name: str, today: str) -> dict[str, Any]:
    """Разбор саммари встречи: факты + задачи пользователя и общие."""
    data = await chat_json(
        system=(
            "Ты секретарь. Из протокола встречи извлеки структуру JSON:\n"
            "{"
            '"date": "YYYY-MM-DD или null", '
            '"title": "тема встречи", '
            '"participants": ["имя", ...], '
            '"summary": ["3-5 коротких пунктов содержания"], '
            '"decisions": ["принятые решения"], '
            '"tasks": [{"title": "...", "deadline": "YYYY-MM-DD или null", '
            '"priority": "high|medium|low", "owner": "user|unassigned"}]'
            "}\n"
            f"Имя пользователя на встречах: {user_name or 'неизвестно'}. "
            "В tasks клади только задачи, где ответственный — этот человек, "
            "либо общие задачи без конкретного ответственного (owner=unassigned). "
            "Не бери чужие персональные поручения. "
            f"Сегодня {today}, часовой пояс {TIMEZONE_NAME}. "
            "Относительные сроки («к пятнице», «на следующей неделе») переводи в абсолютные даты. "
            "Если даты встречи нет в тексте — date=null."
        ),
        user=text[:20000],
    )
    if not isinstance(data, dict):
        raise LLMError("Нейросеть вернула неожиданный формат разбора встречи.")
    return data


async def extract_task(text: str, today: str) -> dict[str, Any]:
    data = await chat_json(
        system=(
            "Извлеки одну задачу из фразы пользователя. JSON:\n"
            '{"title": "краткое название", "deadline": "YYYY-MM-DD или null", '
            '"priority": "high|medium|low"}\n'
            "priority=high если срочно/важно/ASAP, low если когда-нибудь/не горит, иначе medium. "
            f"Сегодня {today}, часовой пояс {TIMEZONE_NAME}. "
            "Относительные сроки переводи в абсолютную дату."
        ),
        user=text,
    )
    if not isinstance(data, dict):
        raise LLMError("Не получилось понять задачу.")
    return data


async def match_task_update(text: str, tasks: list[dict[str, Any]], today: str) -> dict[str, Any]:
    payload = [
        {
            "id": t["id"],
            "title": t["title"],
            "deadline": t.get("deadline"),
            "status": t.get("status"),
            "priority": t.get("priority"),
        }
        for t in tasks
        if t.get("status") != "done"
    ]
    data = await chat_json(
        system=(
            "Пользователь хочет изменить существующую задачу. JSON:\n"
            "{"
            '"action": "complete|postpone|delete|in_progress", '
            '"task_ids": ["id", ...], '
            '"ambiguous": true/false, '
            '"new_deadline": "YYYY-MM-DD или null"'
            "}\n"
            "task_ids — id задач из списка, к которым относится фраза. "
            "ambiguous=true, если непонятно какую именно задачу имеют в виду "
            "(несколько похожих или слишком общее описание). "
            "Если совпадение однозначное — один id и ambiguous=false. "
            "Если фраза не относится ни к одной задаче из списка — task_ids: [] и ambiguous=false. "
            f"Сегодня {today}, TZ {TIMEZONE_NAME}. "
            "Для postpone вычисли new_deadline."
        ),
        user=f"Задачи: {json.dumps(payload, ensure_ascii=False)}\nФраза: {text}",
    )
    if not isinstance(data, dict):
        raise LLMError("Не получилось сопоставить фразу с задачами.")
    return data


async def answer_meeting_question(question: str, meetings: list[dict[str, Any]], user_name: str) -> str:
    compact = []
    for m in meetings[-30:]:
        compact.append(
            {
                "date": m.get("date"),
                "title": m.get("title"),
                "participants": m.get("participants"),
                "summary": m.get("summary"),
                "decisions": m.get("decisions"),
            }
        )
    return await chat(
        [
            {
                "role": "system",
                "content": (
                    f"Ты помощник {user_name or 'пользователя'}. Отвечай по-русски кратко и по делу. "
                    "Опирайся только на переданную историю встреч. "
                    "Если информации нет — честно скажи, что в сохранённых встречах этого нет. "
                    "Не выдумывай. "
                    "У каждого факта указывай дату встречи, из которой он взят, в формате "
                    "«22 июля: …» (день и месяц словами)."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"История встреч: {json.dumps(compact, ensure_ascii=False)}\n\n"
                    f"Вопрос: {question}"
                ),
            },
        ],
        temperature=0.3,
    )


async def build_week_plan(
    tasks: list[dict[str, Any]],
    today: str,
    weekday: int,
    days: list[tuple[str, str]],
    occupancy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Распределение открытых задач по Пн–Пт, не больше 4 в день."""
    payload = [
        {
            "id": t["id"],
            "title": t["title"],
            "deadline": t.get("deadline"),
            "priority": t.get("priority"),
            "status": t.get("status"),
        }
        for t in tasks
        if t.get("status") != "done"
    ]
    day_list = ", ".join(f"{key} = {d}" for key, d in days)
    keys_json = ", ".join(f'"{key}": []' for key, _ in days)
    occ_text = ""
    if occupancy:
        occ_text = (
            "\nЗанятость по календарю (рабочее окно 10:00–19:00). "
            "Если свободно меньше 3 часов — ставь в этот день не больше 1–2 задач.\n"
            + json.dumps(occupancy, ensure_ascii=False)
        )
    data = await chat_json(
        system=(
            "Составь план на оставшиеся рабочие дни недели. JSON:\n"
            f'{{"days": {{{keys_json}}}, "comment": "1 короткое предложение"}}\n'
            'Элемент дня: {"id": "...", "note": "подсказка или пустая строка"}.\n'
            f"Доступные дни (только их, прошедшие дни не используй): {day_list}.\n"
            "Правила: максимум 4 задачи в день; задачу с дедлайном ставь не позже дня дедлайна, "
            "лучше на день раньше; срочные и просроченные — в ближайшие дни; "
            "задачи без срока распредели равномерно; не пихай всё в один день. "
            "Используй только id из списка. "
            "note всегда пустая строка — подсказки к задачам не нужны. "
            "comment — одно короткое предложение до 12 слов. "
            "comment пиши от лица ассистента в женском роде или безлично. "
            f"{occ_text}\n"
            f"Сегодня {today}, TZ {TIMEZONE_NAME}."
        ),
        user=json.dumps(payload, ensure_ascii=False),
    )
    if not isinstance(data, dict):
        raise LLMError("Не получилось собрать план на неделю.")
    return data


async def build_review(
    *,
    last_week_done: list[dict[str, Any]],
    last_week_meetings: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]],
    overdue: list[dict[str, Any]],
    style: str,
    today: str,
    current_week_plan: dict[str, Any] | None = None,
) -> str:
    """Итоги недели текстом, который можно переслать руководителю."""
    style_hint = {
        "normal": "Нормальный объём: 1 экран, списки, без воды.",
        "short": "Очень коротко: 6–10 строк, только главное.",
        "long": "Подробнее: чуть больше контекста по встречам и задачам, но всё ещё деловой тон.",
    }.get(style, "Нормальный объём.")
    context = {
        "today": today,
        "completed_last_week": last_week_done,
        "meetings_last_week": last_week_meetings,
        "open_tasks": open_tasks,
        "overdue": overdue,
        "current_week_plan": current_week_plan,
    }
    return await chat(
        [
            {
                "role": "system",
                "content": (
                    "Ты пишешь еженедельный статус на русском, который можно сразу переслать "
                    "руководителю или коллегам. Структура:\n"
                    "1) Итоги прошлой недели (Пн–Вс): что сделано, какие встречи и решения, "
                    "что перенесено или просрочено.\n"
                    "2) Планы на текущую неделю: если передан current_week_plan — опирайся на него "
                    "(это уже согласованный план с /week), не придумывай другой. "
                    "Если плана нет — по открытым задачам.\n"
                    f"{style_hint} Можно умеренно использовать эмодзи. Не выдумывай факты. "
                    "Текст должен быть готов к пересылке: не заканчивай фразами вроде "
                    "«если нужно, могу…», «напиши, если…», не предлагай следующие шаги бота."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False),
            },
        ],
        temperature=0.4,
    )


async def daily_digest_text(
    *,
    name: str,
    today_tasks: list[str],
    overdue: list[str],
    plan_today: list[str],
    meetings_today: list[str] | None = None,
) -> str:
    """Короткая утренняя сводка; если LLM недоступна, вызывающий соберёт запасной текст."""
    return await chat(
        [
            {
                "role": "system",
                "content": (
                    f"Напиши короткое утреннее сообщение на русском для {name or 'коллеги'}. "
                    "Структура: приветствие, просроченное, встречи на сегодня, задачи на сегодня, план дня. "
                    "Если списки пустые — скажи, что день свободный. Без канцелярита."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "overdue": overdue,
                        "meetings_today": meetings_today or [],
                        "today_deadlines": today_tasks,
                        "plan_today": plan_today,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.4,
    )
