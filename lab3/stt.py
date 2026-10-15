"""Распознавание голосовых сообщений (speech-to-text).

Используем тот же OpenAI-совместимый шлюз, что и для текста:
POST {LLM_BASE_URL}/audio/transcriptions (multipart: file + model).
Telegram присылает голосовые в формате .ogg (opus) — Whisper-подобные модели
принимают его напрямую, конвертировать не нужно.

Какая модель распознавания доступна на шлюзе, заранее неизвестно, поэтому
пробуем по очереди: STT_MODEL из .env, затем запасные варианты.
Первую сработавшую модель запоминаем, чтобы дальше не перебирать.
"""

from __future__ import annotations

import logging
import os

import httpx

from config import LLM_API_KEY, LLM_BASE_URL

logger = logging.getLogger(__name__)

_env_model = os.getenv("STT_MODEL", "").strip()
CANDIDATE_MODELS = [m for m in [
    _env_model,
    "openai/gpt-4o-mini-transcribe",
    "openai/whisper-1",
    "whisper-1",
] if m]
# Убираем повторы, сохраняя порядок.
CANDIDATE_MODELS = list(dict.fromkeys(CANDIDATE_MODELS))

_working_model: str | None = None

# Голосовое-реплика (задача, вопрос, пересказ встречи) — до 10 минут.
MAX_VOICE_SECONDS = 600
# Запись целой встречи (аудиофайл) — до 60 минут. Ограничение по размеру задаёт Telegram:
# бот может скачать файл не больше 20 МБ.
MAX_MEETING_SECONDS = 3600
MAX_AUDIO_BYTES = 20 * 1024 * 1024
AUDIO_EXTENSIONS = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".mp4", ".aac")

_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".webm": "audio/webm",
}


class STTError(Exception):
    """Ошибка распознавания, которую можно показать пользователю."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _mime(filename: str) -> str:
    name = filename.lower()
    for ext, mime in _MIME.items():
        if name.endswith(ext):
            return mime
    return "audio/ogg"


async def _try_model(client: httpx.AsyncClient, model: str, audio: bytes, filename: str) -> str | None:
    response = await client.post(
        f"{LLM_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        data={"model": model, "language": "ru"},
        files={"file": (filename, audio, _mime(filename))},
    )
    if response.status_code >= 400:
        # Ключ в лог не попадает — пишем только код ответа и модель.
        logger.warning("STT HTTP %s for model %s", response.status_code, model)
        return None
    try:
        text = response.json().get("text", "")
    except ValueError:
        text = response.text
    return (text or "").strip()


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """Возвращает распознанный текст или бросает STTError."""
    global _working_model
    if not LLM_API_KEY:
        raise STTError("Не задан LLM_API_KEY — голосовые распознать не получится.")

    models = [_working_model] if _working_model else CANDIDATE_MODELS
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            for model in models:
                text = await _try_model(client, model, audio, filename)
                if text is None:
                    continue
                if _working_model != model:
                    logger.info("STT: работает модель %s", model)
                    _working_model = model
                if not text:
                    raise STTError("Не расслышала ни слова. Попробуй записать ещё раз, ближе к микрофону.")
                return text
    except httpx.TimeoutException:
        raise STTError("Распознавание заняло слишком долго. Попробуй голосовое покороче.") from None
    except httpx.HTTPError as exc:
        logger.warning("STT network error: %s", type(exc).__name__)
        raise STTError("Не получилось связаться с сервисом распознавания. Попробуй позже.") from None

    # Запомненная модель перестала работать — в следующий раз переберём все заново.
    _working_model = None
    raise STTError(
        "Пока не получается распознать голосовое — сервис распознавания недоступен. "
        "Напиши, пожалуйста, текстом."
    )
