# Личный ассистент: задачи и встречи

Telegram-бот на Python (`python-telegram-bot` 21+, async). Принимает текстовые саммари встреч и обычные задачи, достаёт поручения через LLM, строит план на неделю, напоминает о сроках и отвечает на вопросы по истории встреч.

## Что умеет

- `/start`, `/setname`, `/help` — знакомство и подсказки
- `.txt` саммари встречи — разбор, история, задачи с кнопками «Добавить все / Выбрать / Пропустить»
- свободный текст — новая задача, смена статуса, вопрос о встрече, план или итоги недели
- `/tasks`, `/done`, `/week`, `/meetings`, `/review`, `/settings`
- уведомления по часовому поясу из `.env` (будни 9:00, напоминания 18:00 и 15:00)

Данные каждого пользователя хранятся отдельно в `data.json` (атомарная запись).

## Запуск на macOS

1. Создай бота в Telegram через [@BotFather](https://t.me/BotFather) и скопируй токен.

2. В каталоге проекта:

```bash
cd lab1
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Скопируй пример настроек и заполни секреты:

```bash
cp .env.example .env
```

В `.env` укажи:

- `TELEGRAM_BOT_TOKEN` — токен от BotFather
- `LLM_BASE_URL` — базовый URL OpenAI-совместимого API, например LLM Gateway курса `https://llmgateway.nikolaitimofeev.ru/v1`
- `LLM_API_KEY` — ключ этого API
- `LLM_MODEL` — имя модели, например `openai/gpt-5.4-mini`
- `TIMEZONE` — часовой пояс, по умолчанию `Asia/Tbilisi`

4. Запусти бота:

```bash
python bot.py
```

В консоли появятся логи (токены туда не пишутся). Остановка — `Ctrl+C`.

## Структура

- `bot.py` — запуск и регистрация обработчиков
- `handlers.py` — команды, текст, файлы, кнопки
- `llm.py` — запросы к LLM через `httpx` (`POST {LLM_BASE_URL}/chat/completions`)
- `storage.py` — JSON-хранилище
- `jobs.py` — расписание уведомлений
- `keyboards.py`, `dates.py`, `config.py` — клавиатуры, даты, настройки
