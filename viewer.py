#!/usr/bin/env python3
"""
Живая панель просмотра логов по одному аккаунту (по умолчанию @LVV_3O).
Показывает: время, событие, ОТ КОГО, КОМУ, текст.

Запуск:
    python3 viewer.py            — аккаунт @LVV_3O (id 1350738338)
    python3 viewer.py 1040241357 — другой аккаунт по account_id
Обновляется каждые 3 секунды. Выход — Ctrl+C.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.db")
DEFAULT_ACC = 1350738338          # @LVV_3O ("лук лер")
ACC = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ACC
LIMIT = 25
TZ_OFFSET = 3                     # часовой пояс (Москва = +3, поменяй при нужде)

EV = {"new": "🆕 новое", "edited": "✏️ измен.", "deleted": "🗑️ удал."}


def fmt_time(lg):
    """UTC из базы -> местное время DD.MM HH:MM:SS."""
    try:
        dt = datetime.fromisoformat(lg) + timedelta(hours=TZ_OFFSET)
        return dt.strftime("%d.%m %H:%M:%S")
    except Exception:  # noqa: BLE001
        return (lg or "")[:16]


def who(name, user):
    name = name or "—"
    return f"{name} (@{user})" if user else name


def load():
    con = sqlite3.connect(DB)
    # имя аккаунта (кому/от кого = сам аккаунт)
    r = con.execute(
        "SELECT account_name FROM messages WHERE account_id=? "
        "AND account_name IS NOT NULL LIMIT 1", (ACC,)).fetchone()
    acct = (r[0] if r else None) or f"id{ACC}"
    # собеседник по каждому чату (берём из входящих)
    partners = {}
    for cid, sn, su in con.execute(
            "SELECT chat_id, sender_name, sender_username FROM messages "
            "WHERE account_id=? AND direction='in'", (ACC,)):
        if cid not in partners and sn:
            partners[cid] = who(sn, su)
    # последние сообщения
    rows = con.execute(
        "SELECT logged_at, chat_id, direction, event, sender_name, "
        "sender_username, text FROM messages WHERE account_id=? "
        "ORDER BY id DESC LIMIT ?", (ACC, LIMIT)).fetchall()
    con.close()
    return acct, partners, rows


def draw():
    os.system("clear")
    try:
        acct, partners, rows = load()
    except Exception as e:  # noqa: BLE001
        print("Ошибка чтения базы:", e)
        return
    print(f"\033[1;36m📋 Панель аккаунта: {acct}  (id {ACC})\033[0m")
    print("обновление каждые 3с · выход — Ctrl+C\n")
    print(f'\033[1m{"время":14}  {"событие":9} {"от кого":20} '
          f'{"кому":20} текст\033[0m')
    print("─" * 124)
    if not rows:
        print("  (пока пусто — от этого аккаунта ещё не было сообщений)")
    for lg, cid, dr, ev, sn, su, tx in rows:
        t = fmt_time(lg)
        e = EV.get(ev, ev or "")
        if dr == "in":
            frm, to = who(sn, su), acct
        else:
            frm, to = acct, partners.get(cid, f"id{cid}")
        frm = frm[:20]
        to = to[:20]
        tx = (tx or "").replace("\n", " ")[:45]
        color = "\033[33m" if ev == "deleted" else (
            "\033[35m" if ev == "edited" else "\033[0m")
        print(f'{color}{t:14}  {e:9} {frm:20} {to:20} {tx}\033[0m')
    print("\n" + "─" * 124)
    now = (datetime.now() + timedelta(hours=TZ_OFFSET - 0)).strftime('%H:%M:%S')
    print(f"последние {LIMIT} записей · обновлено {now} (МСК)")


if __name__ == "__main__":
    try:
        while True:
            draw()
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nвыход.")
