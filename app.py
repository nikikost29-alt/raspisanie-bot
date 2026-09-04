# -*- coding: utf-8 -*-
"""
Вебхук Telegram для команды /raspisanie. Точка входа приложения на Vercel.

Почему файл называется app.py и лежит в корне: Vercel ищет точку входа
Python в app.py / index.py / server.py / main.py / wsgi.py / asgi.py (или в
src/, app/) и берёт из неё переменную `app`. Раскладка с api/*.py и классом
handler у них помечена как «для существующих проектов», и наш деплой по ней
не пошёл: детектор точки входа отработал раньше и упал с "No python
entrypoint found in default locations". Здесь обычное WSGI-приложение —
никакого фреймворка, никакой лишней зависимости.

При такой схеме Vercel отправляет в приложение ВСЕ запросы, поэтому адрес
вебхука может быть любым; оставляем /api/telegram, он говорящий.

Вся логика — в parser.py и bot.py в этом же каталоге, здесь только приём
HTTP, проверка что запрос от Telegram, и защита от повторов.

Переменные окружения (Settings → Environment Variables в проекте Vercel):
    BOT_TOKEN        токен бота
    CHAT_ID          id группы, где бот отвечает
    OWNER_ID         личка владельца: и команды, и сообщения об ошибках
    WEBHOOK_SECRET   произвольная строка; её же передаём в setWebhook
                     параметром secret_token, Telegram шлёт её в заголовке
                     X-Telegram-Bot-Api-Secret-Token
"""

import json
import os
import traceback

import bot

# Файловая система на Vercel только для чтения, писать можно в /tmp.
# Инстанс живёт между вызовами, пока "тёплый", поэтому этого хватает,
# чтобы не ответить дважды на один и тот же update_id.
SEEN_FILE = os.environ.get("SEEN_FILE", "/tmp/raspisanie_seen.json")
SEEN_KEEP = 500

_username = None   # имя бота из getMe, спрашиваем один раз на инстанс
_seen = None       # множество уже обработанных update_id


def bot_username():
    global _username
    if _username is None:
        _username = (bot.get_me().get("username") or "").lower()
    return _username


def seen_ids():
    global _seen
    if _seen is None:
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as fh:
                _seen = set(json.load(fh))
        except (OSError, ValueError, TypeError):
            _seen = set()
    return _seen


def remember(update_id):
    ids = seen_ids()
    ids.add(update_id)
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(sorted(ids)[-SEEN_KEEP:], fh)
    except OSError:
        pass  # не смогли записать — переживём, множество в памяти осталось


def check_secret(secret_header):
    """
    True, если запрос действительно от Telegram.

    Если WEBHOOK_SECRET не задан, проверять нечем — пропускаем всех
    (и говорим об этом в лог, потому что так делать не надо).
    """
    secret = (os.environ.get("WEBHOOK_SECRET") or "").strip()
    if not secret:
        print("ВНИМАНИЕ: WEBHOOK_SECRET не задан, запросы никак не проверяются")
        return True
    return secret_header == secret


def handle_update(update):
    """
    Обработать один апдейт. Возвращает строку с решением — она уходит в лог
    Vercel, чтобы потом было видно, что бот сделал и почему.
    """
    update_id = update.get("update_id")
    message = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or {})
    chat_id = str((message.get("chat") or {}).get("id", ""))

    arg = bot.parse_command(message, bot_username())
    if arg is None:
        return "#%s: не команда, игнор" % update_id

    # свои — группа и личка владельца; из остальных чатов молчим
    allowed = {bot.env("CHAT_ID"), bot.env("OWNER_ID")}
    if chat_id not in allowed:
        return "#%s: команда из чужого чата %s, игнор" % (update_id, chat_id)

    if update_id in seen_ids():
        return "#%s: повтор, уже отвечали" % update_id

    try:
        day, words = bot.resolve_day(arg)
    except ValueError:
        text = bot.HELP
    else:
        text, _ = bot.build(day, words)

    message_id = bot.send(text, chat_id)          # отвечаем в ТОТ ЖЕ чат
    remember(update_id)
    return "#%s: ответил в %s, message_id=%s, %s" % (
        update_id, chat_id, message_id, text.split("\n")[0])


def report_to_owner(error_text):
    """Сообщить владельцу об ошибке. Сам при этом упасть не имеет права."""
    try:
        bot.send("❌ Вебхук расписания упал:\n%s" % error_text, bot.env("OWNER_ID"))
    except Exception as exc:
        print("не смог сообщить владельцу:", exc)


def env_report():
    """
    Какие переменные окружения заведены — только да/нет, без значений.

    Нужно, чтобы диагностировать самую частую беду деплоя (переменные завели
    в одном проекте Vercel, а работает другой), просто открыв адрес в браузере.
    """
    return {name: bool((os.environ.get(name) or "").strip())
            for name in ("BOT_TOKEN", "CHAT_ID", "OWNER_ID", "WEBHOOK_SECRET")}


def app(environ, start_response):
    """
    WSGI-приложение. Telegram ВСЕГДА получает 200: любой другой код заставит
    его повторять доставку этого апдейта снова и снова.
    """
    note = "ok"
    extra = {}
    try:
        if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
            note = "вебхук расписания жив, шлите POST от Telegram"
            extra["env"] = env_report()
        elif not check_secret(
                environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")):
            print("запрос с неверным X-Telegram-Bot-Api-Secret-Token, игнор")
            note = "ignored"
        else:
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            raw = environ["wsgi.input"].read(length) if length else b""
            update = json.loads(raw.decode("utf-8") or "{}")
            note = handle_update(update)
            print(note)
    except Exception as exc:
        trace = traceback.format_exc()
        print(trace)
        report_to_owner(trace[-1500:])
        # В ответ кладём только тип и текст ошибки (без трейса и без значений
        # переменных) — иначе причину не увидеть, не имея доступа к логам.
        note = "error: %s: %s" % (type(exc).__name__, exc)
        extra["env"] = env_report()

    payload = {"ok": True, "note": note}
    payload.update(extra)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response("200 OK", [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


# псевдоним на случай, если Vercel возьмёт точку входа как WSGI application
application = app
