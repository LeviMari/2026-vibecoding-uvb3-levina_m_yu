"""Настройки бота из переменных окружения."""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Tbilisi").strip() or "Asia/Tbilisi"

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = ZoneInfo("Asia/Tbilisi")
    TIMEZONE_NAME = "Asia/Tbilisi"

# Лимит саммари встречи: 300 КБ достаточно для длинного протокола.
MAX_TXT_BYTES = 300 * 1024

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(_BASE_DIR, "data.json")

# Путь к SQLite: на хостинге можно положить базу на постоянный диск.
_db_raw = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"
DB_PATH = _db_raw if os.path.isabs(_db_raw) else os.path.join(_BASE_DIR, _db_raw)

# Telegram user_id администратора для /admin. Пустая строка — команда никому не доступна.
_admin_raw = os.getenv("ADMIN_ID", "").strip()
try:
    ADMIN_ID = int(_admin_raw) if _admin_raw else None
except ValueError:
    ADMIN_ID = None
