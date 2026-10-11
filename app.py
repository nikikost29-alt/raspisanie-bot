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
import homework

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
    # свои — группа и личка владельца; из остальных чатов молчим
    allowed = {bot.env("CHAT_ID"), bot.env("OWNER_ID")}

    # Нажатие на кнопку под сообщением бота — отдельный тип апдейта.
    # Приходит, только если в setWebhook разрешён "callback_query".
    callback = update.get("callback_query")
    if callback:
        chat_id = str(((callback.get("message") or {}).get("chat") or {})
                      .get("id", ""))
        if chat_id not in allowed:
            return "#%s: кнопка из чужого чата %s, игнор" % (update_id, chat_id)
        if update_id in seen_ids():
            return "#%s: повтор, уже отвечали" % update_id
        note = homework.on_callback(callback)
        remember(update_id)
        return "#%s: %s" % (update_id, note or "кнопка не наша")

    message = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or {})
    chat_id = str((message.get("chat") or {}).get("id", ""))

    parsed = bot.parse_command(message, bot_username())
    is_homework = parsed is None and homework.wants(message, bot_username())
    if parsed is None and not is_homework:
        return "#%s: не команда, игнор" % update_id

    if chat_id not in allowed:
        return "#%s: сообщение из чужого чата %s, игнор" % (update_id, chat_id)

    if update_id in seen_ids():
        return "#%s: повтор, уже отвечали" % update_id

    if is_homework:
        # «!дз …» или ответ на вопрос бота «по какому предмету»:
        # homework сам отвечает в чат, нам остаётся записать в лог
        note = homework.on_message(message, update_id, bot_username())
        remember(update_id)
        return note or "#%s: про дз, но отвечать нечего" % update_id

    command, arg = parsed

    if command in bot.HELP_COMMANDS:
        text = bot.help_text()
        message_id = bot.send(text, chat_id)
        remember(update_id)
        return "#%s: %s -> ответил в %s, message_id=%s" % (
            update_id, command, chat_id, message_id)

    if command in bot.HOMEWORK_COMMANDS:
        # с какой даты смотреть дз — как у /raspisanie без аргумента:
        # до 19:00 с сегодняшнего дня, после — с завтрашнего
        start, _ = bot.resolve_day(bot.COMMAND, "")
        text = homework.summary(start)
        message_id = bot.send(text, chat_id)
        remember(update_id)
        return "#%s: %s -> ответил в %s, message_id=%s, %s" % (
            update_id, command, chat_id, message_id, text.split("\n")[0])

    try:
        day, words = bot.resolve_day(command, arg)
    except ValueError:
        text = bot.HELP
    else:
        text, _ = bot.build(day, words)
        text += homework.block_for(day)

    message_id = bot.send(text, chat_id)          # отвечаем в ТОТ ЖЕ чат
    remember(update_id)
    return "#%s: %s %r -> ответил в %s, message_id=%s, %s" % (
        update_id, command, arg, chat_id, message_id, text.split("\n")[0])


def report_to_owner(error_text, title="Вебхук расписания упал"):
    """Сообщить владельцу об ошибке. Сам при этом упасть не имеет права."""
    try:
        bot.send("❌ %s:\n%s" % (title, error_text), bot.env("OWNER_ID"))
    except Exception as exc:
        print("не смог сообщить владельцу:", exc)


def env_report():
    """
    Какие переменные окружения заведены — только да/нет, без значений.

    Нужно, чтобы диагностировать самую частую беду деплоя (переменные завели
    в одном проекте Vercel, а работает другой), просто открыв адрес в браузере.
    """
    return {name: bool((os.environ.get(name) or "").strip())
            for name in ("BOT_TOKEN", "CHAT_ID", "OWNER_ID", "WEBHOOK_SECRET",
                         "GITHUB_TOKEN", "CRON_SECRET")}


CRON_PATHS = {"/api/cron/evening": False, "/api/cron/last": True}


def run_cron(environ, start_response):
    """
    Вечерняя рассылка по расписанию Vercel (crons в vercel.json).

    Vercel сам присылает заголовок «Authorization: Bearer <CRON_SECRET>».
    Без него запускать нельзя — иначе любой, кто узнает адрес, сможет
    дёргать рассылку. Если CRON_SECRET не завели, рассылка не идёт, и
    владелец получает об этом сообщение.
    """
    last = CRON_PATHS[environ.get("PATH_INFO", "")]
    secret = (os.environ.get("CRON_SECRET") or "").strip()
    status, note = "200 OK", ""
    if not secret:
        note = "cron: в Vercel не задан CRON_SECRET, рассылка не запущена"
        report_to_owner("в Vercel не задана переменная CRON_SECRET. "
                        "Заведи её и сделай Redeploy.",
                        "Вечерняя рассылка не запустилась")
        status = "401 Unauthorized"
    elif environ.get("HTTP_AUTHORIZATION", "") != "Bearer " + secret:
        note = "cron: неверный секрет, игнор"
        status = "401 Unauthorized"
    else:
        try:
            note = "cron%s: %s" % (" (последний)" if last else "",
                                   bot.cron(last))
        except Exception:
            trace = traceback.format_exc()
            print(trace)
            report_to_owner(trace[-1500:], "Вечерняя рассылка упала")
            note = "cron: ошибка, подробности у владельца"
            status = "500 Internal Server Error"
    print(note)
    body = json.dumps({"ok": status.startswith("200"), "note": note},
                      ensure_ascii=False).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def app(environ, start_response):
    """
    WSGI-приложение. Telegram ВСЕГДА получает 200: любой другой код заставит
    его повторять доставку этого апдейта снова и снова.

    Отдельно — адреса вечерней рассылки /api/cron/…: их зовёт не Telegram,
    а расписание Vercel.
    """
    if environ.get("PATH_INFO", "") in CRON_PATHS:
        return run_cron(environ, start_response)

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
