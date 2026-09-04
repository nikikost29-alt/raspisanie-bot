# -*- coding: utf-8 -*-
"""
Прописать адрес вебхука в Telegram, не копируя токен руками.

Токен и WEBHOOK_SECRET берутся из .env.local (или из переменных окружения),
так что ничего подставлять в ссылку не нужно.

    python set_webhook.py https://raspisanie-bot.vercel.app/api/telegram
    python set_webhook.py --info      # только показать, что сейчас настроено
    python set_webhook.py --dry URL   # показать, что будет сделано, и выйти

drop_pending_updates=true ставится намеренно: пока вебхука не было, в очереди
Telegram копились сообщения, и без этого бот ответил бы разом на все.
"""

import sys

import requests

import bot

API = "https://api.telegram.org/bot%s/%s"
TIMEOUT = 30


def call(method, **params):
    resp = requests.get(API % (bot.env("BOT_TOKEN"), method),
                        params=params, timeout=TIMEOUT)
    return resp.json()


def show_info():
    info = call("getWebhookInfo").get("result", {})
    print("сейчас настроено:")
    print("  url:                  %r" % info.get("url", ""))
    print("  pending_update_count: %s" % info.get("pending_update_count"))
    print("  allowed_updates:      %s" % info.get("allowed_updates"))
    if info.get("last_error_message"):
        print("  ПОСЛЕДНЯЯ ОШИБКА:     %s (%s)"
              % (info["last_error_message"], info.get("last_error_date")))
    return info


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = [a for a in argv[1:] if not a.startswith("--")]
    dry = "--dry" in argv

    if "--info" in argv:
        show_info()
        return 0

    if not args:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    url = args[0].rstrip("/")

    secret = bot.env("WEBHOOK_SECRET")
    print("ставлю вебхук:")
    print("  url          = %s" % url)
    print("  secret_token = %s… (%d символов, из .env.local)"
          % (secret[:4], len(secret)))
    print("  токен бота взят из .env.local, в ссылке его печатать не нужно")

    if dry:
        print("\n--dry: ничего не отправлено")
        return 0

    result = call(
        "setWebhook",
        url=url,
        secret_token=secret,
        allowed_updates='["message"]',
        drop_pending_updates="true",
    )
    print("\nответ Telegram:", result)
    if not result.get("ok"):
        return 1

    print()
    info = show_info()
    if info.get("url") == url:
        print("\nГотово. Пиши /raspisanie в группе.")
        return 0
    print("\nАдрес в getWebhookInfo не совпал с тем, что ставили.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
