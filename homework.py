# -*- coding: utf-8 -*-
"""
Домашние задания: ловим в чате «дз!», понимаем предмет, напоминаем накануне.

Как это работает:
  1. Кто-то пишет в группе «дз! матеша §5».
  2. Вебхук вызывает catch(), тот находит слово «матеша» в словарике сленга,
     понимает, что речь про Математику, и ищет ближайший день, когда этот
     предмет стоит в расписании.
  3. Запись кладётся в homework.json.
  4. Вечером рассылка зовёт block_for(день) и дописывает к расписанию
     строчку «не забудь сделать дз».

Где лежит homework.json:
  * на Vercel писать на диск нельзя, поэтому вебхук читает и пишет файл
    в репозитории через GitHub API (нужны GITHUB_TOKEN и GITHUB_REPO);
  * в GitHub Actions и локально файл просто лежит рядом и читается с диска.
"""

import base64
import json
import os
import re
from datetime import date, datetime, timedelta

import requests

import parser as schedule

MARKER = "дз!"          # ключевое слово в чате
LOOKAHEAD_DAYS = 14     # на сколько дней вперёд ищем предмет в расписании
KEEP_DAYS = 3           # сколько дней держим уже прошедшие записи

HOMEWORK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "homework.json")
GITHUB_API = "https://api.github.com/repos/%s/contents/%s"
GITHUB_PATH = "homework.json"
TIMEOUT = 30

# Словарик сленга: как пишут в чате -> кусок настоящего названия предмета.
# Справа именно КУСОК слова без окончания, чтобы ловились все падежи.
# Дописывать сюда свои слова можно и нужно.
ALIASES = {
    "матеша": "математик", "матан": "математик", "матика": "математик",
    "физра": "физическая культура", "физ-ра": "физическая культура",
    "физкультура": "физическая культура", "фізра": "физическая культура",
    "инглиш": "английск", "англ": "английск", "инглишь": "английск",
    "инфа": "информатик", "информа": "информатик",
    "бд": "баз данных", "базы": "баз данных", "базаданных": "баз данных",
    "история": "истори", "истор": "истори",
    "философия": "философ", "филос": "философ",
    "экономика": "эконом", "эконом": "эконом",
    "стандарты": "стандартизац", "стандарт": "стандартизац",
    "внедрение": "внедрен", "вис": "внедрен",
    "интеллект": "интеллектуальн", "иси": "интеллектуальн",
    "кураторский": "кураторск", "кураторка": "кураторск", "кураторс": "кураторск",
    "безопасность": "безопасност", "иб": "безопасност",
    "право": "прав", "правоведение": "прав",
    "русский": "русск", "литература": "литератур",
}

MIN_WORD = 4  # слова короче — не пытаемся искать как название предмета


# --------------------------------------------------------------------------
# Разбор сообщения
# --------------------------------------------------------------------------

def note_from(text):
    """
    Текст после «дз!» либо None, если маркера в сообщении нет.

    Маркер ищем где угодно в сообщении и без учёта регистра: и «дз! матеша»,
    и «Народ, ДЗ! по матеше параграф 5» одинаково подойдут.
    """
    if not text:
        return None
    low = text.lower().replace("ё", "е")
    pos = low.find(MARKER)
    if pos < 0:
        return None
    return text[pos + len(MARKER):].strip()


def _norm(text):
    """В нижний регистр, ё->е, знаки препинания в пробелы."""
    low = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^а-яa-z0-9]+", " ", low).strip()


def _alias_stems(note):
    """Куски названий из словарика сленга — им верим в первую очередь."""
    found = []
    for word in _norm(note).split():
        alias = ALIASES.get(word)
        if alias and alias not in found:
            found.append(alias)
    return found


def _word_stems(note):
    """
    Просто длинные слова заметки, чтобы «стандартизация» нашлась и без
    отдельной строчки в ALIASES. Слабее словарика: такие слова как
    «системы» встречаются сразу в нескольких предметах.
    """
    found = []
    for word in _norm(note).split():
        if len(word) >= MIN_WORD:
            stem = word[:-2] if len(word) > 6 else word  # грубо срезаем окончание
            if stem not in found:
                found.append(stem)
    return found


# фамилия с инициалами: "Снетков А.В."
TEACHER_RE = re.compile(r"\s[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]?\.?")
# хвосты вида "ауд. 307", "лек.", "пр.зан.", "Спортивный зал №116"
TAIL_RE = re.compile(
    r"\s+(ауд\.|каб\.|лек\.|лаб\.|пр\.зан\.|пр\.|спортивный зал)", re.IGNORECASE)


def subject_of(lesson_text):
    """
    Название предмета из строки пары.

    Обычно оно идёт до первой запятой, но в строках вроде «Разговоры о важном
    Снетков А.В. ауд. 307» запятой нет — тогда отрезаем фамилию с инициалами
    и хвост с аудиторией, иначе они попадут в название.
    """
    text = (lesson_text or "").split(",")[0].strip()
    for pattern in (TEACHER_RE, TAIL_RE):
        m = pattern.search(text)
        if m:
            text = text[:m.start()]
    return text.strip(" .,")


def match_subject(note, lessons):
    """Название предмета из этих пар, про который говорит заметка. Или None."""
    for stems in (_alias_stems(note), _word_stems(note)):
        hit = _scan(stems, lessons)
        if hit:
            return hit
    return None


def _scan(stems, lessons):
    for lesson in lessons:
        subject = subject_of(lesson["text"])
        flat = _norm(subject)
        for stem in stems:
            if stem and stem in flat:
                return subject
    return None


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
                result.append((day, found[2]))
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
    """Список записей. Пусто, если файла ещё нет."""
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


# --------------------------------------------------------------------------
# Что вызывает вебхук
# --------------------------------------------------------------------------

def catch(text, update_id, author, today=None):
    """
    Обработать сообщение с «дз!».

    Возвращает строку-ответ в чат либо None, если маркера в сообщении нет.
    """
    note = note_from(text)
    if note is None:
        return None
    if not note:
        return "После «дз!» напиши, по какому предмету. Например: дз! матеша §5"

    today = today or datetime.now(_tz()).date()
    days = upcoming(today + timedelta(days=1))

    # Сначала прочёсываем ВСЕ дни словариком сленга и только потом — простыми
    # словами заметки. Иначе слабое слово вроде «системы» поймало бы чужой
    # предмет в ближайший день раньше, чем точное совпадение в следующий.
    hit = None
    for stems in (_alias_stems(note), _word_stems(note)):
        for day, lessons in days:
            subject = _scan(stems, lessons)
            if subject:
                hit = (day, subject)
                break
        if hit:
            break

    if hit:
        day, subject = hit
        try:
            items, sha = load()
            items = _prune(items, today)
            if any(i.get("id") == update_id for i in items):
                return None  # этот же апдейт уже записан
            items.append({
                "id": update_id,
                "subject": subject,
                "note": note,
                "author": author,
                "for_date": day.isoformat(),
                "added": datetime.now(_tz()).strftime("%Y-%m-%d %H:%M"),
            })
            save(items, sha)
        except Exception as exc:
            # хранилище не настроено или GitHub не ответил — честно говорим
            # об этом, а не делаем вид, что записали
            print("дз: не смог сохранить: %s" % exc)
            return ("Понял, что это про «%s» на %s, но записать не смог — "
                    "хранилище недоступно. Скажи владельцу бота."
                    % (subject, day.strftime("%d.%m")))
        return "✍️ Записал: %s, %s. Напомню вечером накануне." % (
            subject, day.strftime("%d.%m"))

    known = []
    for day, lessons in days[:6]:
        for lesson in lessons:
            name = subject_of(lesson["text"])
            if name and name not in known:
                known.append(name)
    hint = "\n".join("• " + name for name in known[:8])
    example = known[0].split()[0].lower() if known else "матеша"
    return ("Не понял, по какому предмету. Вот что есть в ближайшие дни:\n%s\n\n"
            "Напиши название предмета, например: дз! %s конспект" % (hint, example))


def block_for(day):
    """
    Строчки «не забудь дз» для этого дня. Пустая строка, если записей нет.

    Зовётся и вечерней рассылкой, и ответом на /tm — но в сравнение текста
    для state.json не попадает, чтобы добавленное дз не выглядело как
    «расписание изменили».
    """
    try:
        items, _ = load()
    except Exception as exc:          # хранилище не настроено или недоступно
        print("дз: не смог прочитать записи: %s" % exc)
        return ""

    mine = [i for i in items if str(i.get("for_date")) == day.isoformat()]
    if not mine:
        return ""

    lines = ["", "📝 Не забудь сделать дз:"]
    for item in mine:
        note = str(item.get("note", "")).strip()
        subject = str(item.get("subject", "")).strip()
        lines.append("• %s — %s" % (subject, note) if note else "• %s" % subject)
    return "\n".join(lines)


def _tz():
    import bot
    return bot.YEKB
