#!/usr/bin/env python3
"""
Живая панель по одному аккаунту (по умолчанию @LVV_3O), РАЗБИТАЯ ПО СОБЕСЕДНИКАМ.
Каждый чат — отдельным блоком. Обновляется каждые 2 секунды.

Запуск:
    python3 viewer.py            — аккаунт @LVV_3O (id 1350738338)
    python3 viewer.py 1040241357 — другой аккаунт по account_id
Выход — Ctrl+C.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.db")
DEFAULT_ACC = 1350738338          # @LVV_3O ("лук лер")
ACC = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ACC
TZ_OFFSET = 3                     # МСК = +3
CHATS_SHOWN = 6                   # сколько собеседников показывать
PER_CHAT = 8                      # сообщений на собеседника
REFRESH = 2                       # секунд между обновлениями


def who(name, user):
    name = name or "—"
    return f"{name} (@{user})" if user else name


def fmt(lg):
    try:
        return (datetime.fromisoformat(lg)
                + timedelta(hours=TZ_OFFSET)).strftime("%d.%m %H:%M")
    except Exception:  # noqa: BLE001
        return (lg or "")[:16]


def load():
    con = sqlite3.connect(DB)
    r = con.execute(
        "SELECT account_name FROM messages WHERE account_id=? "
        "AND account_name IS NOT NULL LIMIT 1", (ACC,)).fetchone()
    acct = (r[0] if r else None) or f"id{ACC}"
    rows = con.execute(
        "SELECT id, chat_id, direction, event, sender_name, sender_username, "
        "text, logged_at FROM messages WHERE account_id=? ORDER BY id",
        (ACC,)).fetchall()
    con.close()
    chats, partner, last_id = {}, {}, {}
    for mid, cid, dr, ev, sn, su, tx, lg in rows:
        chats.setdefault(cid, []).append((dr, ev, sn, su, tx, lg))
        last_id[cid] = mid
        if dr == "in" and sn and cid not in partner:
            partner[cid] = who(sn, su)
    return acct, chats, partner, last_id


def draw():
    os.system("clear")
    try:
        acct, chats, partner, last_id = load()
    except Exception as e:  # noqa: BLE001
        print("Ошибка чтения базы:", e)
        return
    print(f"\033[1;36m📋 {acct} — по собеседникам\033[0m"
          f"   обновление {REFRESH}с · выход Ctrl+C\n")
    if not chats:
        print("  (пусто — от этого аккаунта ещё нет сообщений)")
    order = sorted(chats, key=lambda c: last_id[c], reverse=True)[:CHATS_SHOWN]
    for cid in order:
        name = partner.get(cid, f"чат {cid}")
        print(f"\033[1;33m═══ {name} ═══\033[0m")
        for dr, ev, sn, su, tx, lg in chats[cid][-PER_CHAT:]:
            t = fmt(lg)
            body = (tx or "").replace("\n", " ")[:52]
            if ev == "deleted":
                tag, body = "🗑", f"\033[31m{body}\033[0m"
            elif ev == "edited":
                tag = "✏"
            else:
                tag = " "
            arrow = "◀ " if dr == "in" else "▶ Вы:"
            print(f"  \033[90m{t}\033[0m {arrow}{tag} {body}")
        print()
    print("─" * 72)
    now = (datetime.now(timezone.utc)
           + timedelta(hours=TZ_OFFSET)).strftime("%H:%M:%S")
    print(f"собеседников: {len(chats)} · обновлено {now} (МСК)")


if __name__ == "__main__":
    try:
        while True:
            draw()
            time.sleep(REFRESH)
    except KeyboardInterrupt:
        print("\nвыход.")
