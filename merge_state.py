# -*- coding: utf-8 -*-
"""
Слить две версии state.json.

Нужно воркфлоу: если пока мы работали, кто-то ещё запушил свой state.json,
push отклоняется. Тогда мы подтягиваем чужую версию и сливаем со своей —
вместо того чтобы затирать её или падать.

Правила слияния:
  offset   — берём больший (он только растёт, откат означал бы повторные ответы);
  answered — объединяем, храним хвост;
  sent     — объединяем по датам, при совпадении даты выигрывает более свежий ts.

    python merge_state.py моя.json чужая.json результат.json
"""

import json
import sys

ANSWERED_KEEP = 200


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge(mine, theirs):
    out = {}
    out["offset"] = max(mine.get("offset", 0), theirs.get("offset", 0))

    answered = list(theirs.get("answered", [])) + [
        uid for uid in mine.get("answered", [])
        if uid not in set(theirs.get("answered", []))
    ]
    answered.sort()
    out["answered"] = answered[-ANSWERED_KEEP:]

    sent = dict(theirs.get("sent", {}))
    for day, record in (mine.get("sent") or {}).items():
        old = sent.get(day)
        if not old or str(record.get("ts", "")) >= str(old.get("ts", "")):
            sent[day] = record
    out["sent"] = sent
    return out


def main(argv):
    if len(argv) != 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    result = merge(load(argv[1]), load(argv[2]))
    with open(argv[3], "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    print("слито: offset=%s, отвеченных id=%d, дат в sent=%d"
          % (result["offset"], len(result["answered"]), len(result["sent"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
