# -*- coding: utf-8 -*-
"""
Файлы бота в репозитории — homework.json и state.json.

На Vercel своего диска нет (точнее, он только для чтения), поэтому там
файлы читаются и пишутся через GitHub API — каждая запись становится
коммитом в репозиторий. Так работает, когда задан GITHUB_TOKEN.

В GitHub Actions и локально токена нет: файл просто лежит рядом и
читается с диска, а в репозиторий его отправляет сам воркфлоу.
"""

import base64
import os

import requests

GITHUB_API = "https://api.github.com/repos/%s/contents/%s"
DEFAULT_REPO = "nikikost29-alt/raspisanie-bot"
TIMEOUT = 30


class Conflict(Exception):
    """Файл на GitHub успели поменять, пока мы его правили."""


def github():
    """(токен, репозиторий). Токена нет — значит, работаем с диском."""
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or DEFAULT_REPO).strip()
    return (token or None), repo


def remote():
    """Пишем ли мы в репозиторий через API (то есть работаем на Vercel)."""
    return github()[0] is not None


def _headers(token):
    return {"Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json"}


def gh_read(path):
    """
    (текст, sha) файла в репозитории; ("", None), если файла ещё нет.

    sha — версия файла. При записи GitHub сверит её и откажет, если кто-то
    успел записать раньше нас, — так чужая правка не затрётся молча.
    """
    token, repo = github()
    resp = requests.get(GITHUB_API % (repo, path), headers=_headers(token),
                        timeout=TIMEOUT)
    if resp.status_code == 404:
        return "", None
    resp.raise_for_status()
    payload = resp.json()
    return base64.b64decode(payload["content"]).decode("utf-8"), payload.get("sha")


def gh_write(path, text, sha=None, message="бот: запись"):
    """
    Записать файл в репозиторий одним коммитом.

    [skip ci] в сообщении — чтобы коммит не запускал GitHub Actions, а
    ignoreCommand в vercel.json не даёт ему пересобирать проект на Vercel.
    """
    token, repo = github()
    data = {
        "message": message + " [skip ci]",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    if sha:
        data["sha"] = sha
    resp = requests.put(GITHUB_API % (repo, path), headers=_headers(token),
                        json=data, timeout=TIMEOUT)
    if resp.status_code in (409, 422):
        raise Conflict("GitHub: %s успели поменять (HTTP %s)"
                       % (path, resp.status_code))
    if resp.status_code not in (200, 201):
        raise RuntimeError("GitHub не принял %s: HTTP %s %s"
                           % (path, resp.status_code, resp.text[:200]))
