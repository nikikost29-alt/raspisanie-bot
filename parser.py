# -*- coding: utf-8 -*-
"""
Парсер расписания НФ УУНиТ для группы ИС-41-23к.

Как это работает:
  1. Качаем страницу расписания https://nf.uust.ru/timetable/fulltime/
  2. Ищем на ней ссылку, в имени которой есть "SPO" (имя меняется каждую неделю,
     поэтому оно нигде не захардкожено). Делаем ссылку абсолютной.
  3. Качаем этот файл и декодируем ЯВНО из windows-1251.
  4. Разворачиваем таблицу в матрицу: объединённые ячейки (colspan/rowspan)
     дублируются во все клетки, которые занимают, — иначе колонки съедут.
  5. Находим строку с названиями групп и номер колонки нужной группы.
  6. Собираем пары на нужную дату.

Запуск:  python parser.py 04.09.26
"""

import re
import sys
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# BeautifulSoup парсер: lxml быстрее, но на некоторых хостингах (в частности
# в Vercel) его колесо может не встать. Тогда молча берём встроенный
# html.parser — он есть в стандартной библиотеке всегда.
try:
    import lxml  # noqa: F401  (проверяем только наличие)

    SOUP_PARSER = "lxml"
except ImportError:  # pragma: no cover
    SOUP_PARSER = "html.parser"

INDEX_URL = "https://nf.uust.ru/timetable/fulltime/"
GROUP = "ИС-41-23к"
TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}

# дд.мм.гг — так даты записаны и в именах файлов, и внутри таблицы
DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{2})\b")
# "1 пара", "10 пара"
PARA_RE = re.compile(r"^\s*(\d+)\s*пара", re.IGNORECASE)


class ParserError(Exception):
    """Общая ошибка парсера."""


class GroupNotFound(ParserError):
    """Колонка нужной группы не найдена в таблице."""


class ScheduleFileNotFound(ParserError):
    """На странице расписания нет ни одной ссылки с "SPO" в имени."""


# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------

def _get(url):
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def fetch_index():
    """Скачать индексную страницу расписания. Возвращает (html, url)."""
    resp = _get(INDEX_URL)
    # Кодировка страницы объявлена в заголовке (UTF-8). Если сервер её не
    # прислал, requests молча подставляет ISO-8859-1 — на неё полагаться нельзя.
    enc = resp.encoding
    if not enc or enc.lower() in ("iso-8859-1", "latin-1"):
        enc = resp.apparent_encoding or "utf-8"
    return resp.content.decode(enc, errors="replace"), resp.url


def fetch_timetable(url):
    """
    Скачать файл расписания и декодировать ЯВНО из windows-1251.

    Проверяем результат: если попались символы-замены "�" или в тексте нет
    слова "пара" — значит кодировка не та, пробуем utf-8. Молчаливые
    кракозябры недопустимы: если и utf-8 не подошёл — падаем с ошибкой.
    """
    resp = _get(url)

    def looks_broken(text):
        return "�" in text or "пара" not in text

    text = resp.content.decode("windows-1251", errors="replace")
    if not looks_broken(text):
        return text

    alt = resp.content.decode("utf-8", errors="replace")
    if not looks_broken(alt):
        return alt

    raise ParserError(
        "Не удалось прочитать %s: ни windows-1251, ни utf-8 не дают "
        "осмысленный текст (нет слова 'пара' или есть битые символы)" % url
    )


# --------------------------------------------------------------------------
# Индексная страница
# --------------------------------------------------------------------------

def find_spo_links(index_html, base_url=INDEX_URL):
    """
    Все ссылки, в имени которых есть "SPO" (регистр не важен), в виде списка
    словарей: {"url": абсолютный url, "href": как в html, "text": подпись,
    "start": date|None, "end": date|None}.

    Имя файла вида (1)-01.09.26-05.09.26-SPO.html — из него достаём период
    недели, чтобы понять, какой файл соответствует запрошенной дате.
    """
    soup = BeautifulSoup(index_html, SOUP_PARSER)
    found = [(a["href"].strip(), _norm(a.get_text(" ")))
             for a in soup.find_all("a", href=True)]

    # Подстраховка: на сайте атрибут пишут как  href ="..." (с пробелом).
    # Парсер такое переваривает, но если вдруг нет — берём ссылки регуляркой.
    if not any("spo" in href.lower() for href, _ in found):
        found += [
            (m.group(1).strip(), "")
            for m in re.finditer(
                r"""<a[^>]*?href\s*=\s*["']([^"']+)["']""", index_html, re.IGNORECASE
            )
        ]

    links = []
    seen = set()
    for href, text in found:
        if "spo" not in href.lower() and "spo" not in text.lower():
            continue
        url = urljoin(base_url, href)  # ссылка всегда абсолютная
        if url in seen:
            continue
        seen.add(url)
        period = _period_from_name(href) or _period_from_name(text) or (None, None)
        links.append(
            {
                "url": url,
                "href": href,
                "text": text,
                "start": period[0],
                "end": period[1],
            }
        )
    return links


def find_updated_at(index_html):
    """Строка вида "данные на 02.09.2026, 15:36:46" с индексной страницы."""
    text = BeautifulSoup(index_html, SOUP_PARSER).get_text(" ")
    m = re.search(r"данные\s+на\s*([^)<]+)", text, re.IGNORECASE)
    if not m:
        return None
    return _norm("данные на " + m.group(1))


def _period_from_name(name):
    """Из "(1)-01.09.26-05.09.26-SPO.html" достать (01.09.2026, 05.09.2026)."""
    found = DATE_RE.findall(name or "")
    if len(found) < 2:
        return None
    try:
        return _parse_date(found[0]), _parse_date(found[1])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Таблица -> матрица
# --------------------------------------------------------------------------

def build_matrix(table):
    """
    Развернуть <table> в матрицу строк-списков с учётом colspan и rowspan.

    Объединённая ячейка дублируется во все клетки, которые занимает, — без
    этого колонки в соседних строках съезжают и расписание берётся не то.
    """
    matrix = []
    pending = {}  # колонка -> [сколько строк ещё занимает, текст]

    for tr in table.find_all("tr"):
        # строки вложенных таблиц (если вдруг появятся) — не наши
        if tr.find_parent("table") is not table:
            continue

        row = {}
        # сначала занимаем клетки, "протянутые" сверху через rowspan
        for col, state in pending.items():
            row[col] = state[1]

        new_pending = {}
        col = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while col in row:  # пропускаем колонки, занятые сверху
                col += 1
            colspan = _int_attr(cell, "colspan")
            rowspan = _int_attr(cell, "rowspan")
            value = _norm(cell.get_text(" "))
            for k in range(colspan):
                row[col + k] = value
                if rowspan > 1:
                    new_pending[col + k] = [rowspan - 1, value]
            col += colspan

        # уменьшаем счётчики старых rowspan-ов, потом добавляем новые
        for c in list(pending):
            pending[c][0] -= 1
            if pending[c][0] <= 0:
                del pending[c]
        pending.update(new_pending)

        width = max(row) + 1 if row else 0
        matrix.append([row.get(i, "") for i in range(width)])

    return matrix


def _int_attr(cell, name):
    try:
        value = int(str(cell.get(name, "1")).strip())
    except (TypeError, ValueError):
        return 1
    return value if value > 0 else 1


# --------------------------------------------------------------------------
# Поиск группы
# --------------------------------------------------------------------------

def find_group_column(matrix, group=GROUP):
    """
    Найти строку с названиями групп и колонки нужной группы.

    Возвращает (row_index, cols, name_as_on_site), где cols — все колонки,
    которые занимает группа (в шапке у неё colspan=2).

    Если группы нет — GroupNotFound. Соседнюю колонку наугад НЕ берём.
    """
    target = _key(group)
    for r, row in enumerate(matrix):
        for c, value in enumerate(row):
            if _key(value) != target:
                continue
            cols = [c]
            k = c + 1
            while k < len(row) and _key(row[k]) == target:
                cols.append(k)
                k += 1
            return r, cols, _norm(value)
    raise GroupNotFound("Колонка группы %r не найдена в таблице расписания" % group)


def _norm(text):
    """Схлопнуть пробелы (включая неразрывные) и обрезать края."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _key(text):
    """Ключ для сравнения названий групп: без лишних пробелов, без регистра."""
    return _norm(text).casefold()


# --------------------------------------------------------------------------
# Разбор дня
# --------------------------------------------------------------------------

def _parse_date(value):
    """Принимает date/datetime или строку дд.мм.гг / дд.мм.гггг."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _norm(value).replace("/", ".").replace("-", ".")
    for fmt in ("%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("Не понимаю дату %r, нужен формат дд.мм.гг" % value)


def _row_date(row, limit):
    """Дата из служебных колонок слева от группы."""
    for value in row[:limit]:
        if DATE_RE.fullmatch(_norm(value)):
            try:
                return _parse_date(_norm(value))
            except ValueError:
                return None
    return None


def _row_para(row, limit):
    """Номер пары из служебных колонок слева от группы."""
    for value in row[:limit]:
        m = PARA_RE.match(_norm(value))
        if m:
            return int(m.group(1))
    return None


def _row_weekday(row, limit):
    """Подпись дня недели ("Пт.") из служебных колонок слева от группы."""
    for value in row[:limit]:
        text = _norm(value)
        if not text or text.isdigit():
            continue
        if DATE_RE.fullmatch(text) or PARA_RE.match(text):
            continue
        if len(text) <= 12:
            return text
    return None


def extract_day(timetable_html, target, group=GROUP):
    """
    Достать пары группы на дату target из уже скачанного файла расписания.

    Возвращает (group_name, weekday, lessons) либо None, если такой даты
    в этом файле нет.
    """
    soup = BeautifulSoup(timetable_html, SOUP_PARSER)

    last_error = None
    for table in soup.find_all("table"):
        matrix = build_matrix(table)
        try:
            header_row, cols, group_name = find_group_column(matrix, group)
        except GroupNotFound as exc:
            last_error = exc
            continue

        first_col = min(cols)
        weekday = None
        lessons = {}  # номер пары -> список текстов (подгруппы — разные тексты)
        seen_date = False

        # одна пара занимает 3 строки, текст в них дублируется
        for row in matrix[header_row + 1:]:
            if len(row) <= first_col:
                continue
            if _row_date(row, first_col) != target:
                continue
            seen_date = True
            if weekday is None:
                weekday = _row_weekday(row, first_col)
            num = _row_para(row, first_col)
            if num is None:
                continue
            # У группы 2 колонки. Обычно текст в них одинаковый (одна пара на
            # всех) — тогда запись одна. Если тексты РАЗНЫЕ, это подгруппы:
            # склеивать их нельзя, отдаём отдельными записями с тем же num.
            texts = lessons.setdefault(num, [])
            for col in cols:
                text = _norm(row[col]) if col < len(row) else ""
                if text and text not in texts:
                    texts.append(text)

        if not seen_date:
            return None
        return (
            group_name,
            weekday,
            [
                {"num": n, "text": text}
                for n in sorted(lessons)
                for text in lessons[n]
            ],
        )

    raise last_error or GroupNotFound(
        "В файле расписания нет ни одной таблицы с группой %r" % group
    )


def get_day(day, group=GROUP):
    """
    Расписание группы на конкретный день.

    day — строка "04.09.26" (или "04.09.2026") либо datetime.date.

    Возвращает словарь:
        {
            "group":   "ИС-41-23к",                  # как написано на сайте
            "date":    "04.09.26",
            "weekday": "Пт.",
            "lessons": [{"num": 3, "text": "..."}],  # только непустые
            "updated": "данные на 02.09.2026, 15:36:46",
            "source":  "https://nf.uust.ru/.../(1)-01.09.26-05.09.26-SPO.html",
        }

    Если недели с этой датой ещё нет на сайте — None (без исключения).
    Если нет колонки группы — GroupNotFound.
    """
    target = _parse_date(day)

    index_html, base_url = fetch_index()
    links = find_spo_links(index_html, base_url)
    if not links:
        raise ScheduleFileNotFound("На %s нет ссылки с 'SPO' в имени" % INDEX_URL)
    updated = find_updated_at(index_html)

    # Сначала файлы, у которых период в имени накрывает нужную дату.
    matching = [
        link for link in links
        if link["start"] and link["end"] and link["start"] <= target <= link["end"]
    ]
    if matching:
        candidates = matching
    elif any(link["start"] and link["end"] for link in links):
        # периоды разобрались, но нужной даты среди них нет — недели ещё нет
        return None
    else:
        # имена нестандартные — проверяем все файлы по содержимому
        candidates = links

    for link in candidates:
        found = extract_day(fetch_timetable(link["url"]), target, group)
        if found is None:
            continue
        group_name, weekday, lessons = found
        return {
            "group": group_name,
            "date": target.strftime("%d.%m.%y"),
            "weekday": weekday,
            "lessons": lessons,
            "updated": updated,
            "source": link["url"],
        }
    return None


# --------------------------------------------------------------------------
# Запуск из консоли
# --------------------------------------------------------------------------

def _print_day(day):
    result = get_day(day)
    if result is None:
        print("Расписания на %s пока нет на сайте."
              % _parse_date(day).strftime("%d.%m.%y"))
        return 0

    head = "%s - %s" % (result["group"], result["date"])
    if result["weekday"]:
        head += " (%s)" % result["weekday"]
    print(head)
    if result["updated"]:
        print(result["updated"])
    print("-" * len(head))
    if not result["lessons"]:
        print("Пар нет.")
    for lesson in result["lessons"]:
        print("%d пара: %s" % (lesson["num"], lesson["text"]))
    return 0


def main(argv):
    # В консоли Windows кодировка по умолчанию не UTF-8 — кириллица падала бы.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    day = argv[1] if len(argv) > 1 else date.today()
    try:
        return _print_day(day)
    except ValueError as exc:
        print("Ошибка: %s" % exc, file=sys.stderr)
        return 1
    except GroupNotFound as exc:
        print("Ошибка: %s" % exc, file=sys.stderr)
        return 2
    except ParserError as exc:
        print("Ошибка: %s" % exc, file=sys.stderr)
        return 3
    except requests.RequestException as exc:
        print("Сайт не отвечает: %s" % exc, file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv))
