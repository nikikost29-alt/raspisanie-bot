# -*- coding: utf-8 -*-
"""
Домашние задания: ловим в чате «!дз», понимаем предмет и срок, напоминаем.

Два вида записей:

  * обычное дз — «!дз стандартизация работа на юрайте». Срока нет, значит
    сдавать на ближайшей паре этого предмета. Бот ищет её в расписании и
    напоминает вечером накануне.

  * дз со сроком — «!дз курсовая по уфис до 15.12» (или «до 15 декабря»).
    Например, курсовая, на которую дают месяц. Бот напоминает в каждом
    сообщении с расписанием, начиная за неделю до срока.

Как бот узнаёт предмет — см. таблицу SUBJECTS ниже. Плюс он сам понимает
полное название и сокращение по первым буквам («уфис», «попд», «уабд»).

Где лежит homework.json:
  * на Vercel писать на диск нельзя, поэтому вебхук читает и пишет файл
    в репозитории через GitHub API (нужен GITHUB_TOKEN);
  * в GitHub Actions и локально файл просто лежит рядом и читается с диска.
"""

import base64
import json
import os
import re
from datetime import date, datetime, timedelta

import requests

import parser as schedule

# --------------------------------------------------------------------------
# СЛОВАРЬ ПРЕДМЕТОВ — сюда можно и нужно дописывать свои слова
# --------------------------------------------------------------------------
#
# Слева — название ровно как в расписании (до первой запятой).
# Справа — как ещё его могут написать в чате.
#
# Писать сюда НЕ нужно (бот понимает сам):
#   * полное название: «устройство и функционирование информационной системы»
#   * сокращение по первым буквам: «уфис», «уабд», «попд», «итпсис», «ист»
#   * падежи: «правовому», «базам данных», «внедрению»
#
# Нужно дописывать сленг, который из названия не угадать: «физра», «инженерка».
# Короткие слова (до 4 букв: «бд», «итп») бот ищет только целиком, длинные —
# с любым окончанием.

SUBJECTS = {
    "Устройство и функционирование информационной системы": [
        "устройство ис", "устройство информационной системы",
    ],
    "Управление и автоматизация баз данных": [
        "бд", "базы данных", "субд",
    ],
    "Инженерно-техническая поддержка сопровождения информационных систем": [
        "итп", "инженерка", "поддержка ис", "сопровождение ис",
    ],
    "Правовое обеспечение профессиональной деятельности": [
        "право", "правовое", "правовуха", "правоведение",
    ],
    "Стандартизация": [
        "стандартизация, сертификация и техническое документоведение",
        "стандарты", "стандарт", "сертификация",
    ],
    "Интеллектуальные системы и технологии": [
        "иси", "интеллект", "интеллектуалка", "интеллектуальные системы",
    ],
    "Внедрение информационных систем": [
        "внедрение", "внедрение ис",
    ],
    "Физическая культура": [
        "физра", "физ-ра", "физкультура",
    ],
    "Разговоры о важном": [
        "разговоры", "рово",
    ],
    "Тематический кураторский час": [
        "кураторский", "кураторка", "кураторский час", "классный час",
    ],
}

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

MARKER = "!дз"          # так пишем в подсказках
# Понимаем и «!дз», и «дз!» — на деле пишут и так, и так. Буквы вокруг
# не допускаются, чтобы не цеплять куски других слов.
MARKER_RE = re.compile(
    r"(?<![а-яёa-z0-9])(?:!\s?дз|дз\s?!)(?![а-яёa-z0-9])", re.IGNORECASE)

LOOKAHEAD_DAYS = 14     # на сколько дней вперёд ищем ближайшую пару предмета
REMIND_BEFORE = 7       # за сколько дней до срока начинаем напоминать
KEEP_DAYS = 3           # сколько дней держим уже прошедшие записи

HOMEWORK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "homework.json")
GITHUB_API = "https://api.github.com/repos/%s/contents/%s"
GITHUB_PATH = "homework.json"
TIMEOUT = 30

LESSON, DEADLINE = "lesson", "deadline"

# Слова про само задание, а не про предмет. Ищи мы предмет и по ним, «работу»
# однажды поймала бы «День методической работы», а «практическую» — практику.
STOP_WORDS = {
    "сделать", "сделай", "делать", "доделать", "переделать", "надо", "нужно",
    "работа", "работу", "работы", "задание", "задания", "задача", "задачи",
    "написать", "напиши", "прочитать", "прочитай", "выучить", "выучи",
    "повторить", "решить", "ответить", "подготовить", "подготовиться",
    "реферат", "доклад", "конспект", "параграф", "презентация", "презентацию",
    "лаба", "лабу", "лабораторная", "лабораторную", "практическая",
    "практическую", "тест", "тесты", "вопросы", "упражнение", "номер",
    "страница", "страницы", "юрайт", "юрайте", "домашка", "домашнее",
    "курсовая", "курсовую", "курсовой", "курсач", "проект", "сдать", "сдача",
    "срок", "завтра", "сегодня", "всем", "кстати",
}
# Служебные слова: в сокращение по первым буквам их можно не брать
# («Устройство и функционирование…» -> и «уфис», и «уифис»).
SMALL_WORDS = {"и", "в", "во", "на", "о", "об", "по", "с", "со", "к", "для",
               "из", "от", "до", "а"}

WEEKDAYS = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]

# --------------------------------------------------------------------------
# Маркер и срок
# --------------------------------------------------------------------------


def find_marker(text):
    """
    (текст после маркера, маркер_в_начале) либо None, если маркера нет.

    Маркер — «!дз» или «дз!», где угодно в сообщении, без учёта регистра.
    Где он стоит, важно: «!дз физра справка» — точно запись о дз, а
    «кто сделал дз! ура» посреди болтовни — скорее всего нет.
    """
    if not text:
        return None
    m = MARKER_RE.search(text)
    if not m:
        return None
    return text[m.end():].strip(), not text[:m.start()].strip()


def note_from(text):
    """Текст после «!дз»/«дз!» либо None, если маркера в сообщении нет."""
    found = find_marker(text)
    return None if found is None else found[0]


MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11,
    "декабр": 12,
}
# «до 15.12», «к 15.12.26», «срок 15.12» — у цифровой даты нужен предлог,
# иначе «параграф 5.2» превратился бы в 5 февраля
NUM_DATE_RE = re.compile(
    r"(?:^|\s)(?:до|к|на|срок)\s+(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?(?![\d./])",
    re.IGNORECASE)
# «до 15 декабря», «15 декабря» — с названием месяца предлог не обязателен
WORD_DATE_RE = re.compile(
    r"(?:(?:^|\s)(?:до|к|на|срок)\s+|(?:^|\s))(\d{1,2})\s+"
    r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|"
    r"августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])(?![а-яё])",
    re.IGNORECASE)


def parse_deadline(note, today):
    """
    (срок, заметка_без_срока, ошибка).

    Срока нет — (None, note, None). Если дата уже прошла недавно — это скорее
    опечатка, возвращаем ошибку. Если прошла давно («до 15.01», сказанное в
    октябре) — значит, имеется в виду следующий год.
    """
    m = NUM_DATE_RE.search(note)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = WORD_DATE_RE.search(note)
        if not m:
            return None, note, None
        day, year = int(m.group(1)), None
        low = m.group(2).lower()
        month = next(v for k, v in MONTHS.items() if low.startswith(k))

    try:
        if year:
            full = int(year) + (2000 if len(year) == 2 else 0)
            deadline = date(full, month, day)
        else:
            deadline = date(today.year, month, day)
            if deadline < today and (today - deadline).days > 60:
                deadline = date(today.year + 1, month, day)
    except ValueError:
        return None, note, "Не понял дату «%s» — такого числа нет." % m.group(0).strip()

    if deadline < today:
        return None, note, "Срок %s уже прошёл. Проверь дату." % deadline.strftime("%d.%m")

    clean = (note[:m.start()] + " " + note[m.end():]).strip(" ,.-")
    return deadline, re.sub(r"\s{2,}", " ", clean), None


# --------------------------------------------------------------------------
# Узнаём предмет
# --------------------------------------------------------------------------


def _norm(text):
    """В нижний регистр, ё->е, знаки препинания и дефисы в пробелы."""
    low = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^а-яa-z0-9]+", " ", low).strip()


def _stem(word):
    """Грубо отрезаем окончание, чтобы ловились падежи."""
    if len(word) <= 3:
        return word
    if len(word) <= 5:
        return word[:-1]
    return word[:-2]


VOWELS = set("аеёиоуыэюяь")


def _part(word):
    """
    (кусочек, как сравнивать) для одного слова из названия или синонима.

    exact  — слово целиком («ис», «физ»);
    vowel  — короткий корень, после него только окончание с гласной:
             «право» ловит «праву» и «правом», но не «правда»;
    prefix — длинный корень, после него что угодно: «устройст» -> «устройству».
    """
    if len(word) <= 3:
        return word, "exact"
    if len(word) <= 5:
        return _stem(word), "vowel"
    return _stem(word), "prefix"


def _hit(word, part):
    stem, mode = part
    if mode == "exact":
        return word == stem
    if not word.startswith(stem):
        return False
    rest = word[len(stem):]
    return mode == "prefix" or not rest or rest[0] in VOWELS


def _acronyms(phrase):
    """
    Сокращения по первым буквам: «Устройство и функционирование
    информационной системы» -> {"уфис", "уифис"}. Слова через дефис
    («Инженерно-техническая») считаются и раздельно, и как одно слово.
    """
    out = set()
    low = (phrase or "").lower().replace("ё", "е")
    for split_hyphen in (True, False):
        pattern = r"[^а-яa-z0-9]+" if split_hyphen else r"[^а-яa-z0-9-]+"
        words = [w for w in re.split(pattern, low) if w]
        for skip_small in (True, False):
            letters = "".join(w[0] for w in words
                              if not (skip_small and w in SMALL_WORDS))
            if len(letters) >= 2 and len(words) >= 2:
                out.add(letters)
    return out


# фамилия с инициалами: "Снетков А.В."
TEACHER_RE = re.compile(r"\s[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]?\.?")
# хвосты вида "ауд. 307", "лек.", "пр.зан.", "Спортивный зал №116"
TAIL_RE = re.compile(
    r"\s+(ауд\.|каб\.|лек\.|лаб\.|пр\.зан\.|пр\.|урок\.|спортивный зал)",
    re.IGNORECASE)
# явное время в начале: "В 09:00 Тематический кураторский час"
LEAD_TIME_RE = re.compile(r"^В\s+\d{1,2}[:.]\d{2}\s+")


def subject_of(lesson_text):
    """
    Название предмета из строки пары.

    Обычно оно идёт до первой запятой, но в строках вроде «Разговоры о важном
    Снетков А.В. ауд. 307» запятой нет — тогда отрезаем фамилию с инициалами
    и хвост с аудиторией, иначе они попадут в название.
    """
    text = LEAD_TIME_RE.sub("", (lesson_text or "").strip())
    text = text.split(",")[0].strip()
    for pattern in (TEACHER_RE, TAIL_RE):
        m = pattern.search(text)
        if m:
            text = text[:m.start()]
    return text.strip(" .,")


def _keys(names):
    """
    Все способы назвать каждый предмет: [(предмет, [(кусочек, как), ...])].

    Ключ срабатывает, если в заметке нашлись ВСЕ его кусочки.
    """
    phrases = {}
    for name in list(names) + [n for n in SUBJECTS if n not in names]:
        phrases[name] = [name] + list(SUBJECTS.get(name, []))

    # Отличительные слова: корень, с которого не начинается ни одно слово
    # других предметов. «сопровождения» узнаёт ИТП, а «системы» — ничего:
    # «систем…» есть ещё у трёх. Сравниваем со ВСЕМИ известными предметами,
    # а не только с ближайшими днями — иначе, если ИСТ и ВИС на этой неделе
    # нет, «система» вдруг стала бы признаком УФИС.
    words_of = {name: {w for phrase in items for w in _norm(phrase).split()}
                for name, items in phrases.items()}
    owners = {}
    for name in names:
        for word in words_of[name]:
            if len(word) < 7:
                continue
            stem = _stem(word)
            clash = any(w.startswith(stem) or stem.startswith(w)
                        for other, ws in words_of.items() if other != name
                        for w in ws if len(w) >= 4)
            if not clash:
                owners.setdefault(stem, set()).add(name)

    phrases = {name: phrases[name] for name in names}
    keys = []
    for name, items in phrases.items():
        for i, phrase in enumerate(items):
            words = [w for w in _norm(phrase).split() if w not in SMALL_WORDS]
            if not words:
                continue
            if len(words) == 1 and len(words[0]) <= 4:
                keys.append((name, [(words[0], "exact")]))  # «бд», «итп»
            else:
                keys.append((name, [_part(w) for w in words]))
            # Сокращения по первым буквам — только из полных названий:
            # из самого имени предмета и синонимов от трёх слов. Из коротких
            # синонимов вроде «поддержка ис» вышли бы «пи», «си» — мусор.
            if (i == 0 and len(words) >= 2) or len(words) >= 3:
                for acronym in _acronyms(phrase):
                    keys.append((name, [(acronym, "exact")]))
        for stem, who in owners.items():
            if who == {name}:
                keys.append((name, [(stem, "prefix")]))
    return keys


def best_subject(note, names, order=None):
    """
    Какой из предметов names имеется в виду в заметке, или None.

    Побеждает самое длинное совпадение: «устройство и функционирование…»
    весомее, чем «уфис». Точное совпадение слова чуть весомее похожего
    по началу. При равенстве — тот, что раньше стоит в order (у нас это
    порядок появления в расписании).
    """
    words = [w for w in _norm(note).split() if w not in STOP_WORDS]
    if not words:
        return None
    scores = {}
    for name, parts in _keys(names):
        if all(any(_hit(w, p) for w in words) for p in parts):
            score = sum(len(stem) + (2 if mode == "exact" else 0)
                        for stem, mode in parts)
            scores[name] = max(scores.get(name, 0), score)
    if not scores:
        return None
    rank = {n: i for i, n in enumerate(order or names)}
    return sorted(scores, key=lambda n: (-scores[n], rank.get(n, 999)))[0]


# --------------------------------------------------------------------------
# Расписание на несколько дней вперёд, за одну загрузку
# --------------------------------------------------------------------------


def upcoming(start, days=LOOKAHEAD_DAYS):
    """
    [(дата, [пары])] на ближайшие дни. Недельные файлы качаем по одному
    разу, а не отдельно на каждый день.
    """
    index_html, base = schedule.fetch_index()
    links = schedule.find_spo_links(index_html, base)
    pages = {}
    result = []

    for shift in range(days):
        day = start + timedelta(days=shift)
        for link in links:
            if link["start"] and link["end"]:
                if not (link["start"] <= day <= link["end"]):
                    continue
            if link["url"] not in pages:
                pages[link["url"]] = schedule.fetch_timetable(link["url"])
            found = schedule.extract_day(pages[link["url"]], day)
            if found:
                result.append((day, schedule.drop_day_off(found[2])))
                break
    return result


# --------------------------------------------------------------------------
# Хранилище
# --------------------------------------------------------------------------
#
# homework.json:
#   {"items":   [записи о дз],
#    "pending": [дз со сроком, по которым бот спросил предмет и ждёт ответа]}

PENDING_HOURS = 24      # сколько ждём ответа «по какому предмету»

SKIP = object()         # change(): «менять нечего, не записывай»


class Conflict(Exception):
    """Файл на GitHub успели поменять, пока мы его правили."""


def _github():
    """(токен, репозиторий) если вебхук настроен писать в репозиторий."""
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or "nikikost29-alt/raspisanie-bot").strip()
    return (token, repo) if token else (None, repo)


def _doc(raw):
    """Разобрать homework.json, переживая пустой, битый и старый формат."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}
    if isinstance(data, list):          # совсем старый формат — просто список
        data = {"items": data}
    if not isinstance(data, dict):
        data = {}
    items, pending = data.get("items"), data.get("pending")
    return {"items": items if isinstance(items, list) else [],
            "pending": pending if isinstance(pending, list) else []}


def load_doc():
    """
    (документ, sha). sha — версия файла на GitHub: по ней GitHub поймёт,
    если кто-то успел записать раньше нас, и не даст затереть чужую правку.
    """
    token, repo = _github()
    if token:
        resp = requests.get(
            GITHUB_API % (repo, GITHUB_PATH),
            headers={"Authorization": "Bearer %s" % token,
                     "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT)
        if resp.status_code == 404:
            return _doc(""), None
        resp.raise_for_status()
        payload = resp.json()
        raw = base64.b64decode(payload["content"]).decode("utf-8")
        return _doc(raw), payload.get("sha")

    if not os.path.exists(HOMEWORK_FILE):
        return _doc(""), None
    with open(HOMEWORK_FILE, "r", encoding="utf-8") as fh:
        return _doc(fh.read()), None


def load():
    """(список записей, sha) — для тех, кому вопросы в ожидании не нужны."""
    doc, sha = load_doc()
    return doc["items"], sha


def save_doc(doc, sha=None, message="дз: запись из чата"):
    """Записать документ. На Vercel — коммитом в репозиторий, иначе на диск."""
    body = json.dumps(doc, ensure_ascii=False, indent=1)
    token, repo = _github()
    if not token:
        with open(HOMEWORK_FILE, "w", encoding="utf-8") as fh:
            fh.write(body)
        return

    data = {
        "message": message + " [skip ci]",
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
    }
    if sha:
        data["sha"] = sha
    resp = requests.put(
        GITHUB_API % (repo, GITHUB_PATH),
        headers={"Authorization": "Bearer %s" % token,
                 "Accept": "application/vnd.github+json"},
        json=data, timeout=TIMEOUT)
    if resp.status_code in (409, 422):
        raise Conflict("GitHub: файл успели поменять (HTTP %s)" % resp.status_code)
    if resp.status_code not in (200, 201):
        raise RuntimeError("GitHub не принял homework.json: HTTP %s %s"
                           % (resp.status_code, resp.text[:200]))


def change(mutate, message="дз: запись из чата"):
    """
    Прочитать homework.json, поправить функцией mutate(doc), записать.

    mutate возвращает результат — его и вернёт change(). Вернула SKIP —
    менять нечего, писать не будем. Если, пока мы правили, файл кто-то
    поменял (два человека написали дз в одну секунду), GitHub откажет —
    тогда перечитываем свежую версию и пробуем ещё раз.
    """
    last = None
    for _ in range(3):
        doc, sha = load_doc()
        _prune(doc)
        result = mutate(doc)
        if result is SKIP:
            return SKIP
        try:
            save_doc(doc, sha, message)
            return result
        except Conflict as exc:
            last = exc
    raise last


def _prune(doc, today=None):
    """Выбросить давно прошедшие дз и вопросы, на которые так и не ответили."""
    today = today or _today()
    edge = (today - timedelta(days=KEEP_DAYS)).isoformat()
    doc["items"] = [i for i in doc["items"]
                    if str(i.get("for_date", "")) >= edge]
    fresh = (datetime.now(_tz()) - timedelta(hours=PENDING_HOURS)).strftime(
        "%Y-%m-%d %H:%M")
    doc["pending"] = [p for p in doc["pending"]
                      if str(p.get("created", "")) >= fresh]


def _date(item):
    try:
        return date.fromisoformat(str(item.get("for_date")))
    except ValueError:
        return None


def _kind(item):
    return item.get("kind") or LESSON  # у старых записей поля нет


def _now():
    return datetime.now(_tz()).strftime("%Y-%m-%d %H:%M")


def _today():
    return datetime.now(_tz()).date()


def _new_item(item_id, author, subject, note, day, kind):
    return {"id": item_id, "kind": kind, "subject": subject, "note": note,
            "author": author, "for_date": day.isoformat(), "added": _now()}


def _storage_failed(exc, what):
    """Сказать в чат коротко, а владельцу в личку — точную причину."""
    print("дз: не смог сохранить: %s" % exc)
    _tell_owner("❌ Не смог записать дз в homework.json:\n%s: %s\n\n"
                "Проверь GITHUB_TOKEN в Vercel: доступ к репозиторию "
                "raspisanie-bot, Contents: Read and write, после "
                "добавления — Redeploy." % (type(exc).__name__, exc))
    return ("Понял %s, но записать не смог. Подробности отправил владельцу бота."
            % what)


# --------------------------------------------------------------------------
# Что вызывает вебхук
# --------------------------------------------------------------------------
#
# on_message() — сообщение в чате; on_callback() — нажатие на кнопку.
# Оба сами отвечают в Telegram и возвращают строку для лога Vercel
# (или None, если сообщение было не про дз).


def wants(message, username):
    """
    Стоит ли вообще отдавать сообщение в on_message. Проверка дешёвая, без
    чтения homework.json: в нём «!дз» или это ответ на сообщение бота
    (вдруг человек отвечает, по какому предмету его дз).
    """
    text = message.get("text") or message.get("caption") or ""
    return find_marker(text) is not None or _reply_to_us(message, username)


def _reply_to_us(message, username):
    sender = (message.get("reply_to_message") or {}).get("from") or {}
    return bool(sender.get("is_bot")) and (
        not username or (sender.get("username") or "").lower() == username)


def on_message(message, update_id, username=None):
    text = message.get("text") or message.get("caption") or ""
    if find_marker(text) is None:
        if _reply_to_us(message, username):
            return _subject_answer(message)
        return None
    return _catch(message, update_id)


DELETE_WORDS = ("удалить", "удали", "удаление", "убрать", "убери",
                "стереть", "сотри")


def _catch(message, update_id, today=None):
    """
    Сообщение с «!дз»: записать, спросить предмет или открыть удаление.

    Молчит бот, если маркер стоит посреди фразы, а записать нечего:
    «кто сделал дз! ура» — это болтовня, а не запись, и отвечать на неё
    списком предметов было бы навязчиво. Если же сообщение НАЧИНАЕТСЯ
    с маркера, человек явно хотел записать дз, и тогда бот подскажет,
    как написать правильно.
    """
    import bot

    text = message.get("text") or message.get("caption") or ""
    note, at_start = find_marker(text)
    chat_id = (message.get("chat") or {}).get("id")
    reply_to = message.get("message_id")
    sender = message.get("from") or {}
    author = (sender.get("first_name") or "").strip()

    def say(answer, markup=None):
        mid = bot.send(answer, chat_id, reply_markup=markup, reply_to=reply_to)
        return "#%s: дз -> %s (message_id=%s)" % (
            update_id, answer.split("\n")[0], mid)

    if not note:
        if not at_start:
            return None
        return say("После «!дз» напиши предмет и что задали. "
                   "Например: !дз уфис конспект")

    first = _norm(note).split()[0] if _norm(note) else ""
    if at_start and first in DELETE_WORDS:
        query = note.split(None, 1)[1] if len(note.split(None, 1)) > 1 else ""
        return _delete_menu(chat_id, reply_to, sender.get("id"), query,
                            update_id)

    today = today or _today()
    deadline, note, error = parse_deadline(note, today)
    if error:
        return say(error) if at_start else None

    days = upcoming(today + timedelta(days=1))
    live = []                                      # предметы в порядке появления
    for _, lessons in days:
        for lesson in lessons:
            name = subject_of(lesson["text"])
            if name and name not in live:
                live.append(name)

    if deadline:
        # со сроком предмет не обязан стоять в ближайших днях — ищем среди всех
        names = live + [n for n in SUBJECTS if n not in live]
        subject = best_subject(note, names, order=live)
        if subject is None:
            return _ask_subject(message, update_id, note, deadline, days)
        return say(_record(update_id, author, subject, note, deadline, DEADLINE))

    subject = best_subject(note, live, order=live)
    if subject:
        for day, lessons in days:
            if any(subject_of(l["text"]) == subject for l in lessons):
                return say(_record(update_id, author, subject, note, day, LESSON))

    if not at_start:
        return None  # маркер посреди болтовни, предмета нет — молчим
    return _ask_subject(message, update_id, note, None, days)


def _record(item_id, author, subject, note, day, kind):
    """Сохранить запись и вернуть текст ответа в чат."""
    item = _new_item(item_id, author, subject, note, day, kind)

    def add(doc):
        if any(i.get("id") == item_id for i in doc["items"]):
            return SKIP                    # этот же апдейт уже записан
        doc["items"].append(item)
        return True

    try:
        change(add)
    except Exception as exc:
        return _storage_failed(exc, "дз про «%s» на %s"
                               % (subject or "дз", day.strftime("%d.%m")))
    return _recorded_text(item)


def _recorded_text(item, why=""):
    day = _date(item)
    title = item.get("subject") or "без предмета"
    if _kind(item) == LESSON:
        return "✍️ Записал: %s, %s%s. Напомню вечером накануне." % (
            title, day.strftime("%d.%m"), why)
    start = day - timedelta(days=REMIND_BEFORE)
    if start <= _today():
        when = "Буду напоминать в расписании каждый день до срока."
    else:
        when = "Начну напоминать в расписании с %s." % start.strftime("%d.%m")
    return "✍️ Записал: %s, срок %s. %s" % (title, _stamp(day), when)


def _stamp(day):
    return "%s %s" % (WEEKDAYS[day.weekday()], day.strftime("%d.%m"))


# --------------------------------------------------------------------------
# «По какому предмету-то дз?» — когда предмет не узнан
# --------------------------------------------------------------------------


def _question_text(note, deadline):
    if deadline:
        return "По какому предмету-то дз?\n«%s», срок %s" % (note, _stamp(deadline))
    return "По какому предмету-то дз?\n«%s»" % note


def _ask_subject(message, update_id, note, deadline, days):
    """
    Задать вопрос с двумя кнопками и запомнить дз «в ожидании».

    Ключ ожидания — update_id исходного сообщения: он уникален и влезает
    в callback_data кнопки (там лимит 64 байта).

    Вместе с вопросом запоминаем ближайшую дату каждого предмета и ближайший
    учебный день — тогда на ответ «уфис» бот сразу поставит дз на ближайшую
    пару УФИС, не скачивая расписание с сайта ещё раз.
    """
    import bot

    chat_id = (message.get("chat") or {}).get("id")
    sender = message.get("from") or {}
    lesson_days = {}
    for day, lessons in days:
        for lesson in lessons:
            lesson_days.setdefault(subject_of(lesson["text"]), day.isoformat())
    lesson_days.pop("", None)
    school_days = [day for day, lessons in days if lessons]
    next_day = school_days[0] if school_days else _today() + timedelta(days=1)

    keyboard = {"inline_keyboard": [[
        {"text": "Нет предмета", "callback_data": "hw:none:%s" % update_id},
        {"text": "Сейчас напишу", "callback_data": "hw:wait:%s" % update_id},
    ]]}
    question = bot.send(_question_text(note, deadline), chat_id,
                        reply_markup=keyboard, reply_to=message.get("message_id"))

    pending = {
        "id": update_id,
        "kind": DEADLINE if deadline else LESSON,
        "chat_id": chat_id,
        "user_id": sender.get("id"),
        "author": (sender.get("first_name") or "").strip(),
        "message_id": message.get("message_id"),
        "msg_ids": [question],         # ответ на любое из них — это предмет
        "note": note,
        "for_date": deadline.isoformat() if deadline else None,
        "next_day": next_day.isoformat(),
        "lesson_days": lesson_days,
        "created": _now(),
    }

    def add(doc):
        doc["pending"] = [p for p in doc["pending"] if p.get("id") != update_id]
        doc["pending"].append(pending)
        return True

    try:
        change(add, "дз: ждём предмет")
    except Exception as exc:
        bot.call("editMessageText", chat_id=chat_id, message_id=question,
                 text=_storage_failed(exc, "дз «%s»" % note))
        return "#%s: дз -> вопрос задан, но не сохранён" % update_id
    return "#%s: дз -> спросил предмет (message_id=%s)" % (update_id, question)


def _resolve(p, answer=None):
    """
    Во что превращается дз «в ожидании», когда ясен (или не нужен) предмет.

    Возвращает готовую запись и пояснение к дате (может быть пустым).
    answer=None — нажали «Нет предмета».
    """
    lesson_days = p.get("lesson_days") or {}
    names = list(lesson_days) + [n for n in SUBJECTS if n not in lesson_days]
    subject = None
    if answer is not None:
        subject = best_subject(answer, names, order=list(lesson_days)) \
            or _as_subject(answer)

    # у вопросов, заданных до этой версии, поля kind нет, но есть срок
    kind = p.get("kind") or (DEADLINE if p.get("for_date") else LESSON)
    why = ""
    if kind == DEADLINE:
        day = date.fromisoformat(p["for_date"])
    elif subject in lesson_days:
        day = date.fromisoformat(lesson_days[subject])
    else:
        # предмета нет или его нет в ближайшие две недели — ставим на
        # ближайший учебный день, чтобы напоминание вообще когда-то пришло
        day = date.fromisoformat(p.get("next_day") or
                                 (_today() + timedelta(days=1)).isoformat())
        why = " (ближайший учебный день)"
    return _new_item(p["id"], p.get("author"), subject, p.get("note"), day,
                     kind), why


def _subject_answer(message):
    """Человек ответил на вопрос бота названием предмета — записываем дз."""
    import bot

    chat_id = (message.get("chat") or {}).get("id")
    answered = (message.get("reply_to_message") or {}).get("message_id")
    user_id = (message.get("from") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not text:
        return None
    found = {}

    def resolve(doc):
        for p in doc["pending"]:
            if answered in (p.get("msg_ids") or []) and \
                    str(p.get("chat_id")) == str(chat_id):
                if p.get("user_id") != user_id:
                    return SKIP            # отвечает не тот, чьё это дз
                item, why = _resolve(p, text)
                doc["pending"].remove(p)
                if not any(i.get("id") == p["id"] for i in doc["items"]):
                    doc["items"].append(item)
                found.update(item=item, why=why, question=p["msg_ids"][0])
                return True
        return SKIP                        # это ответ боту, но не на вопрос

    try:
        result = change(resolve)
    except Exception as exc:
        bot.send(_storage_failed(exc, "предмет"), chat_id,
                 reply_to=message.get("message_id"))
        return "дз: ответ с предметом не сохранён"
    if result is SKIP:
        return None

    _drop_buttons(chat_id, found["question"])
    answer = _recorded_text(found["item"], found["why"])
    bot.send(answer, chat_id, reply_to=message.get("message_id"))
    return "дз: предмет получен -> %s" % answer


def _as_subject(text):
    """Неизвестный предмет: берём как написали, с большой буквы."""
    text = re.sub(r"\s+", " ", text).strip(" .,!?")[:60]
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------
# «!дз удалить»
# --------------------------------------------------------------------------


def _short_subject(name):
    """Короткое имя для кнопки: длинные названия — сокращением (УФИС, ПОПД)."""
    if not name:
        return "без предмета"
    if len(name) <= 22:
        return name
    words = [w for w in re.split(r"[^а-яёa-z0-9]+", name.lower())
             if w and w not in SMALL_WORDS]
    return "".join(w[0] for w in words).upper()


def _button_text(item):
    note = str(item.get("note") or "").strip()
    text = "%s %s — %s" % (_date(item).strftime("%d.%m"),
                           _short_subject(item.get("subject")), note)
    return "❌ " + (text if len(text) <= 48 else text[:47] + "…")


def _delete_menu(chat_id, reply_to, user_id, query, update_id):
    """Показать дз кнопками — нажатая будет удалена."""
    import bot

    items = [i for i in load()[0] if _date(i) and _date(i) >= _today()]
    items.sort(key=_date)
    title = "Какое дз удалить? Нажми на него:"
    if query and items:
        names = []
        for i in items:
            if i.get("subject") and i["subject"] not in names:
                names.append(i["subject"])
        subject = best_subject(query, names)
        if subject:
            items = [i for i in items if i.get("subject") == subject]
        else:
            title = "Не нашёл дз по «%s». Вот все — какое удалить?" % query

    if not items:
        mid = bot.send("Записанных дз нет — удалять нечего.", chat_id,
                       reply_to=reply_to)
        return "#%s: дз удалить -> пусто (message_id=%s)" % (update_id, mid)

    rows = [[{"text": _button_text(i),
              "callback_data": "hw:del:%s:%s" % (i["id"], user_id)}]
            for i in items[:15]]
    rows.append([{"text": "Отмена", "callback_data": "hw:cancel:0:%s" % user_id}])
    mid = bot.send(title, chat_id, reply_markup={"inline_keyboard": rows},
                   reply_to=reply_to)
    return "#%s: дз удалить -> меню из %d (message_id=%s)" % (
        update_id, len(items), mid)


# --------------------------------------------------------------------------
# Нажатия на кнопки
# --------------------------------------------------------------------------


def on_callback(cb):
    """
    Обработать нажатие. Кнопки бывают:
      hw:none:<id>              «Нет предмета»
      hw:wait:<id>              «Сейчас напишу»
      hw:del:<id>:<кто открыл>  удалить дз
      hw:cancel:0:<кто открыл>  отмена удаления
    """
    parts = (cb.get("data") or "").split(":")
    if len(parts) < 3 or parts[0] != "hw":
        return None                        # не наша кнопка
    action = parts[1]
    try:
        key = int(parts[2])
        owner = int(parts[3]) if len(parts) > 3 else None
    except ValueError:
        return None
    user_id = (cb.get("from") or {}).get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    if action in ("del", "cancel") and owner is not None and user_id != owner:
        _toast(cb, "Это меню открыл другой человек 🙂")
        return "кнопка %s: нажал не тот" % action
    if action == "cancel":
        _edit(chat_id, message_id, "Ок, ничего не удаляю.")
        _toast(cb, "")
        return "кнопка: отмена удаления"
    if action == "del":
        return _press_delete(cb, key, chat_id, message_id)
    if action in ("none", "wait"):
        return _press_subject(cb, action, key, user_id, chat_id, message_id)
    return None


def _press_delete(cb, item_id, chat_id, message_id):
    found = {}

    def remove(doc):
        for item in doc["items"]:
            if item.get("id") == item_id:
                doc["items"].remove(item)
                found["item"] = item
                return True
        return SKIP

    try:
        result = change(remove, "дз: удалено из чата")
    except Exception as exc:
        _toast(cb, "Не смог удалить, подробности у владельца")
        _storage_failed(exc, "удаление")
        return "кнопка удалить: ошибка хранилища"
    if result is SKIP:
        _edit(chat_id, message_id, "Это дз уже удалено.")
        _toast(cb, "Уже удалено")
        return "кнопка удалить: уже нет"
    item = found["item"]
    when = _date(item).strftime("%d.%m")
    _edit(chat_id, message_id, "🗑 Удалил (%s):\n%s" % (when, _line(item)))
    _toast(cb, "Удалил")
    return "кнопка удалить: %s" % item_id


def _press_subject(cb, action, key, user_id, chat_id, message_id):
    import bot

    state = {}

    def check(doc):
        for p in doc["pending"]:
            if p.get("id") == key:
                if p.get("user_id") != user_id:
                    state["why"] = "stranger"
                    return SKIP
                state["p"] = dict(p)
                if action == "none":
                    item, why = _resolve(p, None)
                    doc["pending"].remove(p)
                    if not any(i.get("id") == key for i in doc["items"]):
                        doc["items"].append(item)
                    state.update(item=item, why_day=why)
                    return True
                return SKIP                # «Сейчас напишу»: пока ничего не меняем
        state["why"] = "gone"
        return SKIP

    try:
        change(check)
    except Exception as exc:
        _toast(cb, "Не смог записать, подробности у владельца")
        _storage_failed(exc, "дз без предмета")
        return "кнопка %s: ошибка хранилища" % action

    if state.get("why") == "stranger":
        _toast(cb, "Это вопрос не тебе 🙂")
        return "кнопка %s: нажал не автор" % action
    if state.get("why") == "gone":
        _drop_buttons(chat_id, message_id)
        _toast(cb, "Уже неактуально")
        return "кнопка %s: вопрос уже закрыт" % action

    if action == "none":
        _edit(chat_id, message_id,
              _recorded_text(state["item"], state.get("why_day", "")))
        _toast(cb, "Записал")
        return "кнопка: записал без предмета"

    # «Сейчас напишу»: шлём сообщение, на которое Telegram сразу откроет
    # ответ у автора (force_reply), и запоминаем его как ещё один вопрос
    p = state["p"]
    prompt_text = "Напиши предмет ответом на это сообщение 👇"
    upcoming_names = [n for n in (p.get("lesson_days") or {}) if n]
    if upcoming_names:
        prompt_text += "\nВ ближайшие дни: %s" % ", ".join(
            _short_subject(n) for n in upcoming_names)
    prompt = bot.send(prompt_text, chat_id,
                      reply_markup={"force_reply": True, "selective": True,
                                    "input_field_placeholder": "например, уфис"},
                      reply_to=p.get("message_id"))

    def remember_prompt(doc):
        for q in doc["pending"]:
            if q.get("id") == key:
                q["msg_ids"] = list(q.get("msg_ids") or []) + [prompt]
                return True
        return SKIP

    try:
        change(remember_prompt, "дз: ждём предмет")
    except Exception as exc:
        _storage_failed(exc, "ожидание предмета")
    deadline = date.fromisoformat(p["for_date"]) if p.get("for_date") else None
    _edit(chat_id, message_id,
          _question_text(p.get("note"), deadline) + "\n\nЖду ответ 👇")
    _toast(cb, "")
    return "кнопка: ждём предмет (prompt=%s)" % prompt


def _toast(cb, text):
    """Ответить на нажатие — иначе у человека будут крутиться часики."""
    try:
        import bot
        bot.call("answerCallbackQuery", callback_query_id=cb.get("id"),
                 text=text or None)
    except Exception as exc:
        print("дз: answerCallbackQuery: %s" % exc)


def _edit(chat_id, message_id, text):
    """Поменять текст сообщения; кнопки при этом исчезают."""
    try:
        import bot
        bot.call("editMessageText", chat_id=chat_id, message_id=message_id,
                 text=text)
    except Exception as exc:
        print("дз: editMessageText: %s" % exc)


def _drop_buttons(chat_id, message_id):
    try:
        import bot
        bot.call("editMessageReplyMarkup", chat_id=chat_id,
                 message_id=message_id)
    except Exception as exc:            # «message is not modified» и т.п.
        print("дз: editMessageReplyMarkup: %s" % exc)


# --------------------------------------------------------------------------
# Что показываем
# --------------------------------------------------------------------------


def _line(item, on_day=None):
    """• Предмет — что задали (срок …)"""
    subject = str(item.get("subject") or "").strip()
    note = str(item.get("note") or "").strip()
    text = "%s — %s" % (subject, note) if subject and note else (subject or note)
    if _kind(item) != DEADLINE:
        return "• " + text
    due = _date(item)
    stamp = "%s %s" % (WEEKDAYS[due.weekday()], due.strftime("%d.%m"))
    if on_day is not None and due == on_day:
        return "• %s (срок — в этот день ‼️)" % text
    if on_day is not None:
        return "• %s (срок %s, ещё %d дн.)" % (text, stamp, (due - on_day).days)
    return "• %s (срок %s)" % (text, stamp)


def block_for(day):
    """
    Строчки про дз для сообщения с расписанием на этот день. Пусто, если нечего.

    Обычные дз — те, что сдавать в этот день. Дз со сроком — все, у которых
    до срока осталось не больше REMIND_BEFORE дней.

    Зовётся и вечерней рассылкой, и ответами на /today, /tm, /raspisanie —
    но в сравнение текста для state.json не попадает, чтобы добавленное дз
    не выглядело как «расписание изменили».
    """
    try:
        items, _ = load()
    except Exception as exc:          # хранилище не настроено или недоступно
        print("дз: не смог прочитать записи: %s" % exc)
        return ""

    lessons = [i for i in items if _kind(i) == LESSON and _date(i) == day]
    soon = sorted(
        (i for i in items if _kind(i) == DEADLINE and _date(i)
         and timedelta(0) <= _date(i) - day <= timedelta(days=REMIND_BEFORE)),
        key=_date)

    lines = []
    if lessons:
        lines += ["", "📝 Не забудь сделать дз:"]
        lines += [_line(i) for i in lessons]
    if soon:
        lines += ["", "⏳ Скоро сдавать:"]
        lines += [_line(i, on_day=day) for i in soon]
    return "\n".join(lines)


def summary(start):
    """
    Ответ на /dz: дз на ближайшую дату, когда оно вообще есть, начиная со
    start, и отдельно — все задания со сроком.
    """
    try:
        items, _ = load()
    except Exception as exc:
        print("дз: не смог прочитать записи: %s" % exc)
        return "Не смог прочитать записи о дз. Подробности в логах вебхука."

    lessons = [i for i in items if _kind(i) == LESSON and _date(i)
               and _date(i) >= start]
    deadlines = sorted((i for i in items if _kind(i) == DEADLINE and _date(i)
                        and _date(i) >= start), key=_date)

    lines = []
    if lessons:
        nearest = min(_date(i) for i in lessons)
        lines.append("📝 Дз на %s %s:" % (WEEKDAYS[nearest.weekday()],
                                          nearest.strftime("%d.%m")))
        lines += [_line(i) for i in lessons if _date(i) == nearest]
        later = sorted({_date(i) for i in lessons if _date(i) > nearest})
        if later:
            lines.append("Дальше есть дз ещё на: %s" % ", ".join(
                d.strftime("%d.%m") for d in later))
    if deadlines:
        if lines:
            lines.append("")
        lines.append("⏳ Задания со сроком:")
        lines += [_line(i, on_day=None) for i in deadlines]
    if not lines:
        return "Записанных дз нет 🎉\nЗаписать: !дз уфис конспект"
    return "\n".join(lines)


def _tell_owner(text):
    """Написать владельцу в личку. Сам при этом падать не имеет права."""
    try:
        import bot
        bot.send(text, bot.env("OWNER_ID"))
    except Exception as exc:
        print("дз: не смог написать владельцу: %s" % exc)


def _tz():
    import bot
    return bot.YEKB
