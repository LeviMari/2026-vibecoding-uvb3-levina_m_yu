"""Распознавание голосовых сообщений и записей встреч (speech-to-text).

Используем тот же OpenAI-совместимый шлюз, что и для текста. Пробуем два способа:
1) POST {LLM_BASE_URL}/audio/transcriptions — классический Whisper-подобный API;
2) POST {LLM_BASE_URL}/chat/completions с аудио внутри сообщения (input_audio) —
   так работают мультимодальные модели (например, Gemini), если шлюз не даёт
   отдельного API для речи.
Какой способ и какая модель доступны на шлюзе, заранее неизвестно, поэтому пробуем
по очереди и запоминаем первый сработавший вариант. Список моделей можно задать
в .env: STT_MODEL (для способа 1) и STT_CHAT_MODELS (через запятую, для способа 2).
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

from config import LLM_API_KEY, LLM_BASE_URL

logger = logging.getLogger(__name__)


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


TRANSCRIBE_MODELS = list(dict.fromkeys(
    _env_list("STT_MODEL") + ["openai/gpt-4o-mini-transcribe", "openai/whisper-1", "whisper-1"]
))
CHAT_AUDIO_MODELS = list(dict.fromkeys(
    _env_list("STT_CHAT_MODELS")
    + ["google/gemini-2.5-flash", "google/gemini-2.5-flash-lite", "google/gemini-2.0-flash-001",
       "openai/gpt-4o-audio-preview"]
))

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
_FORMAT = {".mp3": "mp3", ".m4a": "m4a", ".mp4": "m4a", ".aac": "aac", ".wav": "wav", ".webm": "webm"}

# Сработавший вариант: ("transcriptions" | "chat", модель).
_working: tuple[str, str] | None = None
# Что ответил шлюз при последней неудачной попытке — для /admin_stt.
last_errors: list[str] = []

_CHAT_PROMPT = (
    "Расшифруй это аудио дословно на том языке, на котором говорят (обычно русский). "
    "Верни только текст расшифровки, без комментариев, заголовков и кавычек."
)


class STTError(Exception):
    """Ошибка распознавания, которую можно показать пользователю."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _ext(filename: str) -> str:
    name = filename.lower()
    return name[name.rfind("."):] if "." in name else ".ogg"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LLM_API_KEY}"}


def _note_error(kind: str, model: str, response: httpx.Response) -> None:
    # Ключ в лог не попадает — только код ответа и начало текста ошибки.
    snippet = " ".join(response.text.split())[:160]
    line = f"{kind} {model}: HTTP {response.status_code} {snippet}"
    last_errors.append(line)
    logger.warning("STT %s", line)


async def _try_transcriptions(client, model: str, audio: bytes, filename: str) -> str | None:
    response = await client.post(
        f"{LLM_BASE_URL}/audio/transcriptions",
        headers=_headers(),
        data={"model": model, "language": "ru"},
        files={"file": (filename, audio, _MIME.get(_ext(filename), "audio/ogg"))},
    )
    if response.status_code >= 400:
        _note_error("transcriptions", model, response)
        return None
    try:
        text = response.json().get("text", "")
    except ValueError:
        text = response.text
    return (text or "").strip()


async def _try_chat(client, model: str, audio: bytes, filename: str) -> str | None:
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _CHAT_PROMPT},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(audio).decode("ascii"),
                            "format": _FORMAT.get(_ext(filename), "ogg"),
                        },
                    },
                ],
            }
        ],
    }
    response = await client.post(f"{LLM_BASE_URL}/chat/completions", headers=_headers(), json=payload)
    if response.status_code >= 400:
        _note_error("chat", model, response)
        return None
    try:
        text = response.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError, TypeError):
        last_errors.append(f"chat {model}: странный ответ")
        return None
    return str(text).strip().strip("«»\"")


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """Возвращает распознанный текст или бросает STTError."""
    global _working
    if not LLM_API_KEY:
        raise STTError("Не задан LLM_API_KEY — голосовые распознать не получится.")

    if _working:
        attempts = [_working]
    else:
        attempts = [("transcriptions", m) for m in TRANSCRIBE_MODELS] + [
            ("chat", m) for m in CHAT_AUDIO_MODELS
        ]
    last_errors.clear()
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            for kind, model in attempts:
                fn = _try_transcriptions if kind == "transcriptions" else _try_chat
                text = await fn(client, model, audio, filename)
                if text is None:
                    continue
                if _working != (kind, model):
                    logger.info("STT: работает %s / %s", kind, model)
                    _working = (kind, model)
                if not text:
                    raise STTError("Не расслышала ни слова. Попробуй записать ещё раз, ближе к микрофону.")
                return text
    except httpx.TimeoutException:
        raise STTError("Распознавание заняло слишком долго. Попробуй запись покороче.") from None
    except httpx.HTTPError as exc:
        logger.warning("STT network error: %s", type(exc).__name__)
        raise STTError("Не получилось связаться с сервисом распознавания. Попробуй позже.") from None

    # Запомненный вариант перестал работать — в следующий раз переберём все заново.
    _working = None
    raise STTError(
        "Пока не получается распознать голосовое — сервис распознавания недоступен. "
        "Напиши, пожалуйста, текстом."
    )


async def audio_models() -> list[str]:
    """Модели шлюза, похожие на умеющие работать со звуком (для /admin_stt)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{LLM_BASE_URL}/models", headers=_headers())
    response.raise_for_status()
    ids = [m.get("id", "") for m in response.json().get("data", [])]
    keys = ("whisper", "transcri", "audio", "gemini", "voxtral", "speech", "stt")
    return sorted(i for i in ids if any(k in i.lower() for k in keys))


def status() -> str:
    if _working:
        return f"работает: {_working[0]} / {_working[1]}"
    return "рабочий вариант пока не найден"
