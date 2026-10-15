"""Онлайн-таблица в Google Таблицах (по отзыву коллеги: «хочу видеть задачи в онлайн-таблице»).

Как это устроено:
- У бота есть «сервисный аккаунт» Google — технический пользователь с почтой
  вида bot-name@project.iam.gserviceaccount.com. Его ключ (JSON) лежит в переменной
  окружения GOOGLE_SERVICE_ACCOUNT_JSON (сам JSON или он же в base64).
- Пользователь создаёт пустую таблицу, даёт этой почте доступ «Редактор» и присылает ссылку.
- Бот переписывает листы «Задачи» и «Встречи». Раз в минуту проверяет, изменилось ли что-то
  (сравнивает отпечаток содержимого), и обновляет таблицу только при изменениях.

Синхронизация односторонняя: бот → таблица. Правки, сделанные руками в таблице,
бот при следующем обновлении перезапишет.

Библиотека gspread синхронная, поэтому вызываем её через asyncio.to_thread,
чтобы не блокировать бота.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from datetime import timedelta
from typing import Any

from dates import today
from storage import PRIORITY_RU, STATUS_RU

logger = logging.getLogger(__name__)

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})")
TASKS_TITLE = "Задачи"
MEETINGS_TITLE = "Встречи"


class SheetsError(Exception):
    """Ошибка, которую можно показать пользователю."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _load_credentials() -> dict | None:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        info = json.loads(raw)
        if info.get("type") != "service_account" or "client_email" not in info:
            raise ValueError("not a service account key")
        return info
    except Exception:
        # Сам ключ в лог не пишем — только факт ошибки.
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON задан, но не читается как ключ сервисного аккаунта")
        return None


_CREDS = _load_credentials()
_client = None


def is_configured() -> bool:
    return _CREDS is not None


def service_email() -> str:
    return (_CREDS or {}).get("client_email", "")


def parse_sheet_id(text: str) -> str | None:
    match = _SHEET_ID_RE.search(text or "")
    return match.group(1) if match else None


def sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def _get_client():
    global _client
    if _client is None:
        import gspread  # импорт здесь: без настроенной таблицы библиотека не нужна

        _client = gspread.service_account_from_dict(_CREDS)
    return _client


# ---------- Что пишем в таблицу ----------

def _fmt_date(value: Any) -> str:
    s = str(value or "")[:10]
    if len(s) == 10 and s[4] == "-":
        return f"{s[8:10]}.{s[5:7]}.{s[0:4]}"
    return ""


def build_values(user: dict) -> tuple[list[list[str]], list[list[str]]]:
    """Строки для листов «Задачи» и «Встречи»."""
    meetings = {m["id"]: m for m in user.get("meetings") or []}

    def meeting_of(t: dict) -> str:
        mid = t.get("source_meeting_id")
        if not mid:
            src = str(t.get("source") or "")
            mid = src.split(":", 1)[1] if src.startswith("meeting:") else None
        m = meetings.get(mid) if mid else None
        return f"{_fmt_date(m.get('date'))} {m.get('title') or ''}".strip() if m else ""

    tasks = user.get("tasks") or []
    open_tasks = sorted(
        (t for t in tasks if t.get("status") != "done"),
        key=lambda t: (t.get("deadline") or "9999-12-31", t.get("created_at") or ""),
    )
    cutoff = (today() - timedelta(days=30)).isoformat()
    done_tasks = sorted(
        (
            t for t in tasks
            if t.get("status") == "done" and str(t.get("completed_at") or "")[:10] >= cutoff
        ),
        key=lambda t: t.get("completed_at") or "",
        reverse=True,
    )
    d0 = today().isoformat()
    task_rows = [["Статус", "Задача", "Срок", "Приоритет", "Встреча", "Создана", "Выполнена"]]
    for t in open_tasks + done_tasks:
        status = STATUS_RU.get(t.get("status"), t.get("status") or "")
        if t.get("status") != "done" and t.get("deadline") and t["deadline"] < d0:
            status = "просрочена"
        task_rows.append([
            status,
            t.get("title") or "",
            _fmt_date(t.get("deadline")),
            PRIORITY_RU.get(t.get("priority"), t.get("priority") or ""),
            meeting_of(t),
            _fmt_date(t.get("created_at")),
            _fmt_date(t.get("completed_at")),
        ])

    meeting_rows = [["Дата", "Тема", "Участники", "Кратко", "Решения"]]
    for m in sorted(meetings.values(), key=lambda m: m.get("date") or "", reverse=True):
        meeting_rows.append([
            _fmt_date(m.get("date")),
            m.get("title") or "",
            ", ".join(map(str, m.get("participants") or [])),
            "\n".join(f"• {x}" for x in m.get("summary") or []),
            "\n".join(f"• {x}" for x in m.get("decisions") or []),
        ])
    return task_rows, meeting_rows


def fingerprint(values: Any) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------- Запись ----------

# Ширина колонок в пикселях. Текст переносится по словам, поэтому длинные задачи
# и пункты встречи видны целиком — строка просто становится выше.
COLUMN_WIDTHS = {
    TASKS_TITLE: [110, 420, 100, 100, 260, 100, 100],
    MEETINGS_TITLE: [100, 260, 200, 420, 420],
}


def _apply_layout(book, ws, widths: list[int]) -> None:
    """Перенос текста, выравнивание по верху и ширина колонок — одним запросом к API."""
    requests = [
        {
            "repeatCell": {
                "range": {"sheetId": ws.id},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
            }
        }
    ]
    for i, width in enumerate(widths):
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )
    # Высота строк — по содержимому (после переноса текста).
    requests.append(
        {
            "autoResizeDimensions": {
                "dimensions": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 0, "endIndex": ws.row_count}
            }
        }
    )
    book.batch_update({"requests": requests})


def _write_sync(sheet_id: str, task_rows: list, meeting_rows: list) -> None:
    import gspread

    try:
        book = _get_client().open_by_key(sheet_id)
        titles = [ws.title for ws in book.worksheets()]
        if TASKS_TITLE not in titles:
            book.add_worksheet(TASKS_TITLE, rows=200, cols=7, index=0)
        if MEETINGS_TITLE not in titles:
            book.add_worksheet(MEETINGS_TITLE, rows=100, cols=5, index=1)
        # Пустой стандартный лист новой таблицы («Лист1» / «Sheet1») убираем, чтобы не путал.
        for ws in book.worksheets():
            if ws.title in ("Лист1", "Sheet1") and not any(any(c for c in r) for r in ws.get_all_values()):
                book.del_worksheet(ws)

        for title, rows in ((TASKS_TITLE, task_rows), (MEETINGS_TITLE, meeting_rows)):
            ws = book.worksheet(title)
            ws.clear()
            ws.update(range_name="A1", values=rows)
            last_col = chr(ord("A") + len(rows[0]) - 1)
            ws.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
            _apply_layout(book, ws, COLUMN_WIDTHS[title])
    except gspread.exceptions.SpreadsheetNotFound:
        raise SheetsError(
            f"Не вижу таблицу. Открой «Настройки доступа» и добавь редактором {service_email()}"
        ) from None
    except gspread.exceptions.APIError as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("Google Sheets API error %s", code)
        if code in (403, 404):
            raise SheetsError(
                f"Нет доступа к таблице. Добавь редактором {service_email()} "
                "(«Настройки доступа» → ввести почту → «Редактор»)."
            ) from None
        if code == 429:
            raise SheetsError("Google просит подождать — обновлю таблицу чуть позже.") from None
        raise SheetsError("Google Таблицы сейчас не отвечают. Попробую позже.") from None


async def sync_user(user: dict, sheet_id: str) -> str:
    """Записать задачи и встречи пользователя. Возвращает отпечаток записанного."""
    if not is_configured():
        raise SheetsError("Онлайн-таблицы пока не настроены у бота.")
    task_rows, meeting_rows = build_values(user)
    try:
        await asyncio.to_thread(_write_sync, sheet_id, task_rows, meeting_rows)
    except SheetsError:
        raise
    except Exception:
        logger.exception("Ошибка записи в Google Таблицу")
        raise SheetsError("Не получилось обновить таблицу. Попробую позже.") from None
    return fingerprint([task_rows, meeting_rows])
