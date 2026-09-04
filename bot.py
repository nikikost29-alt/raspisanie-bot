# -*- coding: utf-8 -*-
"""
Отправка расписания на завтра в Telegram.

Без фреймворков: только requests + parser.py.

    python bot.py              # обычный запуск: расписание на завтра
    python bot.py --dry        # напечатать в консоль, никуда не отправлять
    python bot.py --dry 02.09.26   # то же, но на конкретную дату (для проверки)
    python bot.py --last       # последний запуск за день: если расписания
                               # ещё нет — предупредить и закрыть день
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import requests

import parser as schedule

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------

# Екатеринбург — UTC+5 круглый год, перевода часов нет.
YEKB = timezone(timedelta(hours=5), "Asia/Yekaterinburg")
try:  # если в системе есть база часовых поясов — берём её
    from zoneinfo import ZoneInfo

    YEKB = ZoneInfo("Asia/Yekaterinburg")
except Exception:  # pragma: no cover - на Windows без пакета tzdata
    pass

# Звонки: номер пары -> (начало, конец)
BELLS = {
    1: ("08:00", "09:35"),
    2: ("09:45", "11:20"),
    3: ("12:00", "13:35"),
    4: ("13:45", "15:20"),
    5: ("15:40", "17:15"),
    6: ("17:25", "19:00"),
    7: ("19:10", "20:45"),
}
NO_BELL = "время уточни"  # для 8-й пары и всего, чего нет в BELLS

WEEKDAYS = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
SUNDAY = 6

DIGITS = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]

# явное время в тексте пары: "В 09:00 Тематический кураторский час"
EXPLICIT_TIME_RE = re.compile(r"(?:^|\s)В\s+(\d{1,2}[:.]\d{2})(?=\s|$)")
LEADING_TIME_RE = re.compile(r"^В\s+\d{1,2}[:.]\d{2}\s+")
# "данные на 02.09.2026, 15:36:46" -> дата и часы:минуты
UPDATED_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\D+(\d{2}:\d{2})")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")

TELEGRAM_API = "https://api.telegram.org/bot%s/sendMessage"
TIMEOUT = 30

# Команда в группе. Разбор команд живёт в вебхуке api/telegram.py, но
# сам разбор и подписи — здесь, чтобы не дублировать логику.
# Имя бота НЕ хардкодим: его спрашивают у getMe.
COMMAND = "/raspisanie"
# До 19:00 по Екатеринбургу "/raspisanie" без аргумента — про сегодня,
# после — про завтра.
SWITCH_HOUR = 19

# Подписи "за какую дату": (слово в шапке, слово в предупреждении)
TOMORROW = ("ЗАВТРА", "на завтра")
TODAY = ("СЕГОДНЯ", "на сегодня")


class BotError(Exception):
    """Ошибка бота, которую надо показать владельцу."""


# --------------------------------------------------------------------------
# Секреты
# --------------------------------------------------------------------------

_env_loaded = False


def _load_env():
    """
    Подтянуть .env.local, если он есть. Если файла нет — значения берутся
    из os.environ (так будет в GitHub Actions).
    """
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # без dotenv просто работаем на os.environ
    load_dotenv(ENV_FILE)


def env(name):
    """Значение переменной окружения или внятная ошибка вместо KeyError."""
    _load_env()
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise BotError("не задан %s (переменная окружения или .env.local)" % name)
    return value


# --------------------------------------------------------------------------
# Формат сообщения
# --------------------------------------------------------------------------

def _digit(num):
    if 0 <= num <= 9:
        return DIGITS[num]
    return "".join(DIGITS[int(ch)] for ch in str(num))


def _head(day):
    return "%s %s" % (WEEKDAYS[day.weekday()], day.strftime("%d.%m"))


def date_words(day):
    """Подписи для произвольной даты: в шапке только сама дата."""
    return (None, "на %s" % day.strftime("%d.%m"))


def _title(day, words):
    label = words[0]
    if label:
        return "%s, %s" % (label, _head(day))
    return _head(day)


def lesson_time(num, text):
    """
    (начало, конец) для пары. Явное время из текста ("В 09:00") важнее звонка.
    Если звонка для такой пары нет — (None, None).
    """
    start, end = BELLS.get(num, (None, None))
    m = EXPLICIT_TIME_RE.search(text or "")
    if m:
        start = m.group(1).replace(".", ":")
        if len(start) == 4:
            start = "0" + start
    return start, end


def _time_label(start, end):
    if start and end:
        return "%s–%s" % (start, end)
    if start:
        return start
    return NO_BELL


def _footer(updated):
    if not updated:
        return None
    m = UPDATED_RE.search(updated)
    if m:
        return "(данные на сайте: %s %s)" % (m.group(1), m.group(2))
    return "(%s)" % updated


def format_message(result, day, words=TOMORROW):
    """Собрать текст сообщения из словаря parser.get_day()."""
    lessons = result["lessons"]
    if not lessons:
        return free_day_message(day, words)

    lines = ["📅 %s — %s" % (_title(day, words), result["group"])]

    body = []
    first_start = None
    first_num = None
    last_end = None
    for lesson in lessons:
        num = lesson["num"]
        text = LEADING_TIME_RE.sub("", lesson["text"]).strip()
        start, end = lesson_time(num, lesson["text"])
        if first_num is None:
            first_num, first_start = num, start
        if end:
            last_end = end
        body.append("%s %s %s" % (_digit(num), _time_label(start, end), text))

    if first_start:
        summary = "⏰ К %s, это %d пара." % (first_start, first_num)
    else:
        summary = "⏰ Первая — %d пара, %s." % (first_num, NO_BELL)
    if last_end:
        summary += " Освободишься в %s." % last_end
    lines.append(summary)
    lines.extend(body)

    footer = _footer(result.get("updated"))
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def free_day_message(day, words=TOMORROW):
    return "📅 %s — пар нет, отдыхаем" % _title(day, words)


def not_published_message(day, words=TOMORROW):
    return "⚠️ Расписание %s ещё не выложили (%s)" % (words[1], _head(day))


# --------------------------------------------------------------------------
# Отправка
# --------------------------------------------------------------------------

def send(text, chat_id):
    """Отправить сообщение в Telegram. Возвращает message_id."""
    resp = requests.post(
        TELEGRAM_API % env("BOT_TOKEN"),
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    try:
        payload = resp.json()
    except ValueError:
        raise BotError("Telegram ответил не-JSON: HTTP %s" % resp.status_code)
    if not payload.get("ok"):
        raise BotError(
            "Telegram отказал: %s" % payload.get("description", resp.status_code)
        )
    return payload["result"]["message_id"]


# --------------------------------------------------------------------------
# Состояние
# --------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"sent": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (ValueError, OSError):
        return {"sent": {}}
    state.setdefault("sent", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


def mark_sent(state, day, text):
    state["sent"][day.isoformat()] = {
        "text": text,
        "ts": datetime.now(YEKB).strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------
# Логика запуска
# --------------------------------------------------------------------------

def tomorrow():
    """Завтрашняя дата по Екатеринбургу, а не по UTC."""
    return datetime.now(YEKB).date() + timedelta(days=1)


def build(day, words=TOMORROW):
    """
    Что отправлять на дату day.

    Возвращает (text, is_schedule): is_schedule=False означает "расписания
    на сайте ещё нет" — такой день отмечать отправленным нельзя.
    """
    result = schedule.get_day(day)
    if result is not None:
        return format_message(result, day, words), True
    # Воскресенья в файлах расписания нет вообще — это выходной, а не
    # "не выложили".
    if day.weekday() == SUNDAY:
        return free_day_message(day, words), True
    return not_published_message(day, words), False


# --------------------------------------------------------------------------
# Команда /raspisanie в группе
# --------------------------------------------------------------------------

def get_me():
    """Кто мы такие по мнению Telegram. Имя бота нигде не хардкодим."""
    resp = requests.get(
        "https://api.telegram.org/bot%s/getMe" % env("BOT_TOKEN"), timeout=TIMEOUT
    )
    try:
        payload = resp.json()
    except ValueError:
        raise BotError("getMe ответил не-JSON: HTTP %s" % resp.status_code)
    if not payload.get("ok"):
        raise BotError(
            "getMe отказал: %s" % payload.get("description", resp.status_code)
        )
    return payload["result"]


def command_token(message):
    """
    Первое слово сообщения, если это команда бота.

    Берём из entities типа bot_command, когда Telegram его разметил, иначе
    просто первое слово текста — на случай, если entities не пришли.
    """
    text = message.get("text") or message.get("caption") or ""
    if not text:
        return None, ""

    entities = message.get("entities") or message.get("caption_entities") or []
    for entity in entities:
        if entity.get("type") != "bot_command" or entity.get("offset") != 0:
            continue
        token = text[: entity.get("length", 0)]
        return token.strip(), text[entity.get("length", 0):].strip()

    parts = text.strip().split(None, 1)
    if not parts:
        return None, ""
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


def parse_command(message, username):
    """
    Аргумент команды /raspisanie (может быть пустой строкой) либо None,
    если это не наша команда.

    username — настоящее имя бота из getMe, в нижнем регистре. Суффикс
    "@имя" сверяем без учёта регистра; голая команда без "@" тоже наша.
    """
    token, arg = command_token(message)
    if not token:
        return None

    head = token.strip().lower()
    if "@" in head:
        head, _, mention = head.partition("@")
        if not username or mention != username:
            return None  # команда адресована другому боту
    if head != COMMAND:
        return None
    return arg


def resolve_day(arg):
    """
    По аргументу команды понять дату и подписи.

    Без аргумента: до 19:00 по Екатеринбургу — сегодня, после — завтра.
    Понимает "сегодня", "завтра" и дату вида 05.09 (или 05.09.26).
    """
    now = datetime.now(YEKB)
    today = now.date()
    text = (arg or "").strip().lower()

    if not text:
        if now.hour < SWITCH_HOUR:
            return today, TODAY
        return today + timedelta(days=1), TOMORROW
    if text.startswith("сегодня"):
        return today, TODAY
    if text.startswith("завтра"):
        return today + timedelta(days=1), TOMORROW

    try:
        day = schedule._parse_date(text)
    except ValueError:
        m = re.fullmatch(r"(\d{1,2})[.,/](\d{1,2})", text)
        if not m:
            raise ValueError(arg)
        day = date(today.year, int(m.group(2)), int(m.group(1)))
    return day, date_words(day)


HELP = (
    "Не понял дату. Пиши так:\n"
    "/raspisanie — на сегодня или на завтра\n"
    "/raspisanie завтра\n"
    "/raspisanie 05.09"
)


# --------------------------------------------------------------------------

def run(argv):
    dry = "--dry" in argv
    last = "--last" in argv

    rest = [a for a in argv[1:] if not a.startswith("-")]
    day = schedule._parse_date(rest[0]) if rest else tomorrow()

    text, is_schedule = build(day)

    if dry:
        print(text)
        return 0

    state = load_state()
    previous = state["sent"].get(day.isoformat())

    if not is_schedule:
        # расписания ещё нет: молчим и не отмечаем дату, чтобы следующий
        # запуск попробовал снова
        if not last:
            return 0
        if previous and previous.get("text") == text:
            return 0
        send(text, env("CHAT_ID"))
        mark_sent(state, day, text)
        save_state(state)
        return 0

    if previous and previous.get("text") == text:
        return 0  # уже отправляли ровно это — тихий выход

    if previous:
        text_to_send = "🔄 Расписание на завтра изменили\n\n" + text
    else:
        text_to_send = text

    send(text_to_send, env("CHAT_ID"))
    mark_sent(state, day, text)
    save_state(state)
    return 0


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        return run(argv)
    except Exception as exc:
        message = "❌ Бот расписания упал:\n%s: %s" % (type(exc).__name__, exc)
        print(message, file=sys.stderr)
        if "--dry" not in argv:
            try:
                send(message, env("OWNER_ID"))
            except Exception as report_exc:
                print("Не смог сообщить владельцу: %s" % report_exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
