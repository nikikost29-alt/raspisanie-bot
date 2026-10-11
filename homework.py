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
    for name in names:
        phrases[name] = [name] + list(SUBJECTS.get(name, []))

    # Отличительные слова: встречаются в названии только одного предмета.
    # «сопровождения» узнаёт ИТП, а «информационных» — ничего, оно у трёх.
    owners = {}
    for name, items in phrases.items():
        for phrase in items:
            for word in _norm(phrase).split():
                if len(word) >= 7:
                    owners.setdefault(_stem(word), set()).add(name)

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


def _github():
    """(токен, репозиторий) если вебхук настроен писать в репозиторий."""
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or "nikikost29-alt/raspisanie-bot").strip()
    return (token, repo) if token else (None, repo)


def load():
    """(список записей, sha файла на GitHub или None). Пусто, если файла нет."""
    token, repo = _github()
    if token:
        resp = requests.get(
            GITHUB_API % (repo, GITHUB_PATH),
            headers={"Authorization": "Bearer %s" % token,
                     "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT)
        if resp.status_code == 404:
            return [], None
        resp.raise_for_status()
        payload = resp.json()
        raw = base64.b64decode(payload["content"]).decode("utf-8")
        return _items(raw), payload.get("sha")

    if not os.path.exists(HOMEWORK_FILE):
        return [], None
    with open(HOMEWORK_FILE, "r", encoding="utf-8") as fh:
        return _items(fh.read()), None


def _items(raw):
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


def save(items, sha=None):
    """Записать список. На Vercel — коммитом в репозиторий, иначе на диск."""
    body = json.dumps({"items": items}, ensure_ascii=False, indent=1)
    token, repo = _github()
    if not token:
        with open(HOMEWORK_FILE, "w", encoding="utf-8") as fh:
            fh.write(body)
        return

    data = {
        "message": "дз: запись из чата [skip ci]",
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
    }
    if sha:
        data["sha"] = sha
    resp = requests.put(
        GITHUB_API % (repo, GITHUB_PATH),
        headers={"Authorization": "Bearer %s" % token,
                 "Accept": "application/vnd.github+json"},
        json=data, timeout=TIMEOUT)
    if resp.status_code not in (200, 201):
        raise RuntimeError("GitHub не принял homework.json: HTTP %s %s"
                           % (resp.status_code, resp.text[:200]))


def _prune(items, today):
    """Выбросить записи, у которых день давно прошёл."""
    edge = (today - timedelta(days=KEEP_DAYS)).isoformat()
    return [i for i in items if str(i.get("for_date", "")) >= edge]


def _date(item):
    try:
        return date.fromisoformat(str(item.get("for_date")))
    except ValueError:
        return None


def _kind(item):
    return item.get("kind") or LESSON  # у старых записей поля нет


# --------------------------------------------------------------------------
# Что вызывает вебхук: запись нового дз
# --------------------------------------------------------------------------


def catch(text, update_id, author, today=None):
    """
    Обработать сообщение с «!дз».

    Возвращает строку-ответ в чат либо None — тогда бот молчит.

    Молчит бот, если маркера нет, и ещё если маркер стоит посреди фразы,
    а записать нечего: «кто сделал дз! ура» — это болтовня, а не запись,
    и отвечать на неё списком предметов было бы навязчиво. Если же
    сообщение НАЧИНАЕТСЯ с маркера, человек явно хотел записать дз, и
    тогда бот подскажет, как написать правильно.
    """
    found = find_marker(text)
    if found is None:
        return None
    note, at_start = found
    if not note:
        if not at_start:
            return None
        return "После «!дз» напиши предмет и что задали. Например: !дз уфис конспект"

    today = today or datetime.now(_tz()).date()
    deadline, note, error = parse_deadline(note, today)
    if error:
        return error if at_start else None

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
        return _store(update_id, author, today, subject, note, deadline,
                      DEADLINE)

    subject = best_subject(note, live, order=live)
    if subject:
        for day, lessons in days:
            if any(subject_of(l["text"]) == subject for l in lessons):
                return _store(update_id, author, today, subject, note, day,
                              LESSON)

    if not at_start:
        return None  # маркер посреди болтовни, предмета нет — молчим

    hint = "\n".join("• " + name for name in live[:9])
    return ("Не понял, по какому предмету. Вот что есть в ближайшие дни:\n%s\n\n"
            "Можно писать сокращением: !дз уфис конспект\n"
            "А если есть срок: !дз уфис курсовая до 15.12" % hint)


def _store(update_id, author, today, subject, note, day, kind):
    """Сохранить запись и вернуть ответ в чат."""
    try:
        items, sha = load()
        items = _prune(items, today)
        if any(i.get("id") == update_id for i in items):
            return None  # этот же апдейт уже записан
        items.append({
            "id": update_id,
            "kind": kind,
            "subject": subject,
            "note": note,
            "author": author,
            "for_date": day.isoformat(),
            "added": datetime.now(_tz()).strftime("%Y-%m-%d %H:%M"),
        })
        save(items, sha)
    except Exception as exc:
        # хранилище не настроено или GitHub не ответил — честно говорим
        # об этом в чат, а точную причину отправляем владельцу в личку
        print("дз: не смог сохранить: %s" % exc)
        _tell_owner("❌ Не смог записать дз в homework.json:\n%s: %s\n\n"
                    "Проверь GITHUB_TOKEN в Vercel: доступ к репозиторию "
                    "raspisanie-bot, Contents: Read and write, после "
                    "добавления — Redeploy." % (type(exc).__name__, exc))
        return ("Понял, что это про «%s» на %s, но записать не смог. "
                "Подробности отправил владельцу бота."
                % (subject or "дз", day.strftime("%d.%m")))

    title = subject or "без предмета"
    if kind == LESSON:
        return "✍️ Записал: %s, %s. Напомню вечером накануне." % (
            title, day.strftime("%d.%m"))

    start = day - timedelta(days=REMIND_BEFORE)
    if start <= today:
        when = "Буду напоминать в расписании каждый день до срока."
    else:
        when = "Начну напоминать в расписании с %s." % start.strftime("%d.%m")
    return "✍️ Записал: %s, срок %s %s. %s" % (
        title, WEEKDAYS[day.weekday()], day.strftime("%d.%m"), when)


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
