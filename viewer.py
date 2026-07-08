#!/usr/bin/env python3
"""
Живая панель просмотра логов бота.
Запуск:
    python3 viewer.py            — все аккаунты
    python3 viewer.py 1350738338 — только этот аккаунт (по account_id)
Обновляется каждые 3 секунды. Выход — Ctrl+C.
"""
import os
import sqlite3
import sys
import time

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.db")
ACC = sys.argv[1] if len(sys.argv) > 1 else None
LIMIT = 25

EV = {"new": "🆕 новое", "edited": "✏️ измен.", "deleted": "🗑️ удал."}
DR = {"in": "вход", "out": "исх."}


def fetch():
    q = ("SELECT logged_at, account_name, direction, event, sender_name, text "
         "FROM messages")
    p = ()
    if ACC:
        q += " WHERE account_id=?"
        p = (ACC,)
    q += " ORDER BY id DESC LIMIT ?"
    p = p + (LIMIT,)
    try:
        con = sqlite3.connect(DB)
        rows = con.execute(q, p).fetchall()
        con.close()
        return rows
    except Exception as e:  # noqa: BLE001
        return [("", "", "", "err", "ошибка", str(e))]


def draw():
    os.system("clear")
    title = "📋 TG LOGGER — живой просмотр"
    if ACC:
        title += f"  (аккаунт id={ACC})"
    print("\033[1;36m" + title + "\033[0m")
    print("обновление каждые 3с · выход — Ctrl+C\n")
    print(f'\033[1m{"время":8}  {"аккаунт":14} {"напр":5} {"событие":9} '
          f'{"от кого":16} текст\033[0m')
    print("─" * 110)
    rows = fetch()
    if not rows:
        print("  (пока пусто — нет записей)")
    for lg, an, dr, ev, sn, tx in rows:
        t = (lg or "")[11:19]
        an = (an or "—")[:14]
        d = DR.get(dr, dr or "")
        e = EV.get(ev, ev or "")
        sn = (sn or "—")[:16]
        tx = (tx or "").replace("\n", " ")[:55]
        color = "\033[33m" if ev == "deleted" else (
            "\033[35m" if ev == "edited" else "\033[0m")
        print(f'{color}{t:8}  {an:14} {d:5} {e:9} {sn:16} {tx}\033[0m')
    print("\n" + "─" * 110)
    print(f"показаны последние {LIMIT} записей · {time.strftime('%H:%M:%S')}")


if __name__ == "__main__":
    try:
        while True:
            draw()
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nвыход.")
