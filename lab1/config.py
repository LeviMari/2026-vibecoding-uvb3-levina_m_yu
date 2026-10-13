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

# Ограничение на саммари встречи: 100 КБ достаточно для длинного текстового протокола.
MAX_TXT_BYTES = 100 * 1024
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
