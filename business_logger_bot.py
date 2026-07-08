#!/usr/bin/env python3
"""
Telegram Business Message Logger (aiogram)
==========================================

Обычный бот (токен от @BotFather), который подключается к ЛИЧНЫМ чатам через
функцию Telegram Business и:
  • присылает уведомление, если собеседник УДАЛИЛ или ИЗМЕНИЛ сообщение
    (со старым и новым текстом);
  • сохраняет фото/видео/голосовые/кружки, отправленные "на время" (с таймером);
  • пишет копии всех сообщений в локальную базу SQLite.

Поддерживает НЕСКОЛЬКО подключённых аккаунтов одновременно: уведомления по
каждому аккаунту приходят в ЕГО собственный чат с ботом, а в базе у каждой
записи проставляется, какому аккаунту она принадлежит (account_id).

Требует: Python 3.9+, aiogram>=3.7, python-dotenv
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    BusinessConnection,
    BusinessMessagesDeleted,
    FSInputFile,
)

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "messages.db")
MEDIA_DIR = os.getenv("MEDIA_DIR", "media")
DOWNLOAD_MEDIA = os.getenv("DOWNLOAD_MEDIA", "true").lower() in ("1", "true", "yes")
SAVE_TIMED_MEDIA = os.getenv("SAVE_TIMED_MEDIA", "true").lower() in ("1", "true", "yes")

# Если задан — ВСЕ уведомления идут только в этот чат (общий лог).
# Если пусто — уведомления идут в чат каждого аккаунта отдельно.
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID", "").strip()
LOG_CHAT_ID = int(LOG_CHAT_ID) if LOG_CHAT_ID else None

# БЕЛЫЙ СПИСОК: бот обслуживает только эти аккаунты (по user_id).
# Сообщения/подключения любых других аккаунтов игнорируются.
# Пусто -> разрешены все (не рекомендуется).
_allowed = os.getenv("ALLOWED_ACCOUNTS", "1040241357,1350738338").strip()
ALLOWED_ACCOUNTS = {int(x) for x in _allowed.split(",") if x.strip()}

if not BOT_TOKEN:
    raise SystemExit("Ошибка: не задан BOT_TOKEN в .env (получите у @BotFather)")


def allowed(owner_id) -> bool:
    """True, если аккаунт разрешён (или список пуст)."""
    return (not ALLOWED_ACCOUNTS) or (owner_id in ALLOWED_ACCOUNTS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("business_logger")

Path(MEDIA_DIR).mkdir(parents=True, exist_ok=True)

dp = Dispatcher()

BOT_USERNAME: str = "bot"
TIMED_TYPES = {"photo", "video", "voice", "video_note", "animation"}
# кэш: business_connection_id -> owner_user_id (чей это аккаунт)
CONN_OWNER: dict[str, int] = {}


# --------------------------------------------------------------------------- #
# База данных
# --------------------------------------------------------------------------- #
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_message_id   INTEGER,
            chat_id         INTEGER,
            sender_id       INTEGER,
            sender_name     TEXT,
            sender_username TEXT,
            direction       TEXT,
            text            TEXT,
            media_type      TEXT,
            media_path      TEXT,
            event           TEXT,
            msg_date        TEXT,
            logged_at       TEXT,
            biz_conn_id     TEXT,
            account_id      INTEGER,
            account_name    TEXT
        )
        """
    )
    for col, typ in (("biz_conn_id", "TEXT"), ("account_id", "INTEGER"),
                     ("account_name", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sent (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id INTEGER, message_id INTEGER, sent_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_msg ON messages (chat_id, tg_message_id);"
    )
    conn.commit()
    return conn


DB = init_db()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_config(key: str) -> str | None:
    row = DB.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_config(key: str, value: str) -> None:
    DB.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    DB.commit()


def record_sent(chat_id: int, message_id: int) -> None:
    """Запоминаем id сообщения, отправленного ботом (для команды /clear)."""
    DB.execute(
        "INSERT INTO sent (chat_id, message_id, sent_at) VALUES (?, ?, ?)",
        (chat_id, message_id, now_utc()),
    )
    DB.commit()


def save_row(**kw) -> None:
    fields = (
        "tg_message_id", "chat_id", "sender_id", "sender_name", "sender_username",
        "direction", "text", "media_type", "media_path", "event",
        "msg_date", "logged_at", "biz_conn_id", "account_id", "account_name",
    )
    DB.execute(
        f"INSERT INTO messages ({','.join(fields)}) "
        f"VALUES ({','.join('?' * len(fields))})",
        tuple(kw.get(f) for f in fields),
    )
    DB.commit()


def find_original(chat_id: int, tg_message_id: int):
    """(text, media_type, media_path, sender_name, sender_username, direction)."""
    return DB.execute(
        "SELECT text, media_type, media_path, sender_name, sender_username, direction "
        "FROM messages WHERE chat_id=? AND tg_message_id=? AND event='new' "
        "ORDER BY id ASC LIMIT 1",
        (chat_id, tg_message_id),
    ).fetchone()


# --------------------------------------------------------------------------- #
# Аккаунты (маршрутизация уведомлений по владельцу подключения)
# --------------------------------------------------------------------------- #
def remember_owner(conn_id: str, owner_id: int, owner_name: str | None) -> None:
    CONN_OWNER[conn_id] = owner_id
    set_config(f"conn:{conn_id}", str(owner_id))
    if owner_name:
        set_config(f"conn_name:{conn_id}", owner_name)


async def owner_of(bot: Bot, conn_id: str | None) -> tuple[int | None, str | None]:
    """Вернуть (owner_id, owner_name) для business_connection_id."""
    if not conn_id:
        return None, None
    name = get_config(f"conn_name:{conn_id}")
    if conn_id in CONN_OWNER:
        return CONN_OWNER[conn_id], name
    cached = get_config(f"conn:{conn_id}")
    if cached:
        CONN_OWNER[conn_id] = int(cached)
        return int(cached), name
    try:
        bc = await bot.get_business_connection(conn_id)
        if bc and bc.user:
            nm = " ".join(p for p in [bc.user.first_name, bc.user.last_name] if p)
            remember_owner(conn_id, bc.user.id, nm)
            return bc.user.id, nm
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось определить владельца подключения %s: %s", conn_id, e)
    return None, None


def target_chat(owner_id: int | None) -> int | None:
    if LOG_CHAT_ID is not None:
        return LOG_CHAT_ID
    return owner_id


# --------------------------------------------------------------------------- #
# Хелперы
# --------------------------------------------------------------------------- #
def esc(s: str | None) -> str:
    return html.escape(s or "")


def sender_of(message: Message) -> tuple[int | None, str, str]:
    u = message.from_user
    if not u:
        return None, "Unknown", ""
    name = " ".join(p for p in [u.first_name, u.last_name] if p) or "Unknown"
    return u.id, name, (u.username or "")


def who_html(name: str | None, username: str | None) -> str:
    name = esc(name or "Неизвестно")
    return f"<b>{name}</b> (@{esc(username)})" if username else f"<b>{name}</b>"


def quote(text: str) -> str:
    return f"<blockquote>{esc(text)}</blockquote>"


def acct_tag(owner_name: str | None) -> str:
    return f"👤 Аккаунт: {esc(owner_name)}\n" if owner_name else ""


def media_info(message: Message) -> tuple[str | None, str | None]:
    ct = message.content_type
    mapping = {
        ContentType.PHOTO: lambda m: m.photo[-1].file_id if m.photo else None,
        ContentType.VIDEO: lambda m: m.video.file_id,
        ContentType.VOICE: lambda m: m.voice.file_id,
        ContentType.AUDIO: lambda m: m.audio.file_id,
        ContentType.DOCUMENT: lambda m: m.document.file_id,
        ContentType.VIDEO_NOTE: lambda m: m.video_note.file_id,
        ContentType.ANIMATION: lambda m: m.animation.file_id,
        ContentType.STICKER: lambda m: m.sticker.file_id,
    }
    if ct in mapping:
        try:
            return ct.value, mapping[ct](message)
        except Exception:  # noqa: BLE001
            return ct.value, None
    return (None, None) if ct == ContentType.TEXT else (str(ct), None)


async def download(bot: Bot, file_id: str, name_hint: str) -> str | None:
    try:
        file = await bot.get_file(file_id)
        ext = os.path.splitext(file.file_path or "")[1] or ".bin"
        dest = os.path.join(MEDIA_DIR, f"{name_hint}{ext}")
        await bot.download_file(file.file_path, destination=dest)
        return dest
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось скачать медиа: %s", e)
        return None


async def notify(bot: Bot, target: int | None, text: str) -> None:
    if target is None:
        log.info("(некуда слать — аккаунт не нажал Start) %s",
                 text.replace("\n", " ")[:80])
        return
    try:
        m = await bot.send_message(target, text[:4000])
        record_sent(target, m.message_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось отправить уведомление в %s: %s", target, e)


async def _send_typed(bot: Bot, t: int, mtype: str, media, caption: str) -> None:
    if mtype == "photo":
        m = await bot.send_photo(t, media, caption=caption)
        record_sent(t, m.message_id)
    elif mtype == "video":
        m = await bot.send_video(t, media, caption=caption)
        record_sent(t, m.message_id)
    elif mtype == "voice":
        m = await bot.send_voice(t, media, caption=caption)
        record_sent(t, m.message_id)
    elif mtype == "animation":
        m = await bot.send_animation(t, media, caption=caption)
        record_sent(t, m.message_id)
    elif mtype == "video_note":
        m1 = await bot.send_message(t, caption)
        record_sent(t, m1.message_id)
        m2 = await bot.send_video_note(t, media)
        record_sent(t, m2.message_id)
    else:
        m = await bot.send_document(t, media, caption=caption)
        record_sent(t, m.message_id)


async def send_saved_media(
    bot: Bot, target: int | None, mtype: str, path: str | None,
    caption: str, file_id: str | None = None,
) -> None:
    if target is None:
        return
    if path and os.path.exists(path):
        try:
            await _send_typed(bot, target, mtype, FSInputFile(path), caption)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Пересылка с диска не удалась (%s), пробую file_id", e)
    if file_id:
        try:
            await _send_typed(bot, target, mtype, file_id, caption)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Пересылка по file_id не удалась: %s", e)
    log.warning("Медиа переслать не удалось (%s)", mtype)


# --------------------------------------------------------------------------- #
# Хендлеры
# --------------------------------------------------------------------------- #
@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    if message.from_user and not allowed(message.from_user.id):
        log.info("Чужой /start от id=%s — игнор", message.from_user.id)
        return
    await message.answer(
        "🕵️ <b>Бот-логгер активен!</b>\n\n"
        "Я слежу за вашими личными чатами и пришлю сюда уведомление, если "
        "собеседник <b>удалит</b> 🗑️ или <b>изменит</b> ✏️ сообщение — со старым "
        "и новым текстом. А ещё сохраняю <b>фото, видео, голосовые и кружки, "
        "отправленные с таймером</b> ⏳, пока они не исчезли.\n\n"
        "━━━━━━━━━━━━━━━\n"
        "<b>Как подключить меня к чатам — 3 простых шага:</b>\n\n"
        "1️⃣ Откройте <b>Настройки → Telegram для бизнеса</b>\n"
        "   <i>(нужен Telegram Premium)</i>\n"
        "2️⃣ Найдите пункт <b>«Чат-боты»</b>\n"
        f"3️⃣ Введите <b>@{esc(BOT_USERNAME)}</b> и нажмите <b>«Добавить»</b>\n\n"
        "✅ Готово. Уведомления для этого аккаунта будут приходить именно сюда."
    )
    log.info("Start от чата %s", message.chat.id)


@dp.message(Command("clear"))
async def on_clear(message: Message, bot: Bot) -> None:
    """Удаляет последние сообщения бота в этом чате. /clear [N] (по умолчанию 10)."""
    if message.from_user and not allowed(message.from_user.id):
        return
    parts = (message.text or "").split()
    n = 10
    if len(parts) > 1 and parts[1].isdigit():
        n = max(1, min(int(parts[1]), 100))
    chat_id = message.chat.id
    rows = DB.execute(
        "SELECT id, message_id FROM sent WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, n)).fetchall()
    deleted = 0
    for rid, mid in rows:
        try:
            await bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:  # noqa: BLE001
            pass  # старше 48ч или уже удалено
        DB.execute("DELETE FROM sent WHERE id=?", (rid,))
    DB.commit()
    # удаляем и саму команду /clear
    try:
        await bot.delete_message(chat_id, message.message_id)
    except Exception:  # noqa: BLE001
        pass
    conf = await bot.send_message(
        chat_id, f"🧹 Удалено сообщений бота: {deleted}")
    record_sent(chat_id, conf.message_id)
    log.info("/clear: удалено %s сообщений в чате %s", deleted, chat_id)


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection) -> None:
    state = "подключён ✅" if conn.is_enabled else "отключён ❌"
    if conn.user and not allowed(conn.user.id):
        log.warning("ОТКЛОНЕНО подключение чужого аккаунта id=%s (%s)",
                    conn.user.id, conn.user.first_name)
        return
    if conn.user:
        nm = " ".join(p for p in [conn.user.first_name, conn.user.last_name] if p)
        remember_owner(conn.id, conn.user.id, nm)
        log.info("Business-соединение %s: аккаунт %s (id=%s)",
                 state, nm, conn.user.id)
    else:
        log.info("Business-соединение %s", state)


@dp.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    conn_id = message.business_connection_id
    owner_id, owner_name = await owner_of(bot, conn_id)
    if not allowed(owner_id):
        return
    target = target_chat(owner_id)

    sid, name, username = sender_of(message)
    if message.from_user and message.chat and message.from_user.id == message.chat.id:
        direction = "in"
    else:
        direction = "out"

    mtype, file_id = media_info(message)
    media_path = None
    if DOWNLOAD_MEDIA and file_id and mtype != "sticker":
        media_path = await download(
            bot, file_id, f"{message.chat.id}_{message.message_id}"
        )

    text = message.text or message.caption or ""
    save_row(
        tg_message_id=message.message_id, chat_id=message.chat.id, sender_id=sid,
        sender_name=name, sender_username=username, direction=direction, text=text,
        media_type=mtype, media_path=media_path, event="new",
        msg_date=message.date.isoformat() if message.date else None,
        logged_at=now_utc(), biz_conn_id=conn_id, account_id=owner_id,
        account_name=owner_name,
    )
    body = text or ("[" + (mtype or "media") + "]")
    log.info("[%s|%s] %s (@%s): %s", owner_name or "?", direction, name,
             username or "—", body[:50])

    if SAVE_TIMED_MEDIA and direction == "in" and mtype in TIMED_TYPES:
        label = "🔥 Исчезающее фото сохранено" if mtype == "photo" \
            else f"⏳ Сохранённое медиа ({esc(mtype)})"
        cap = f"{acct_tag(owner_name)}{label}\nОт: {who_html(name, username)}"
        await send_saved_media(bot, target, mtype, media_path, cap, file_id=file_id)


@dp.edited_business_message()
async def on_edited(message: Message, bot: Bot) -> None:
    conn_id = message.business_connection_id
    owner_id, owner_name = await owner_of(bot, conn_id)
    if not allowed(owner_id):
        return
    target = target_chat(owner_id)

    original = find_original(message.chat.id, message.message_id)
    old_text = original[0] if original else "(оригинал не найден)"
    sid, name, username = sender_of(message)
    new_text = message.text or message.caption or ""

    save_row(
        tg_message_id=message.message_id, chat_id=message.chat.id, sender_id=sid,
        sender_name=name, sender_username=username, direction="in", text=new_text,
        media_type=media_info(message)[0], media_path=None, event="edited",
        msg_date=None, logged_at=now_utc(), biz_conn_id=conn_id,
        account_id=owner_id, account_name=owner_name,
    )
    log.info("✏️ [%s] %r -> %r", owner_name or "?", old_text[:30], new_text[:30])
    await notify(
        bot, target,
        f"{acct_tag(owner_name)}"
        f"{who_html(name, username)} изменил(а) сообщение:\n\n"
        f"Old:\n{quote(old_text or '(пусто)')}\n\n"
        f"New:\n{quote(new_text or '(пусто)')}\n\n"
        f"@{esc(BOT_USERNAME)}",
    )


@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted, bot: Bot) -> None:
    conn_id = event.business_connection_id
    owner_id, owner_name = await owner_of(bot, conn_id)
    if not allowed(owner_id):
        return
    target = target_chat(owner_id)
    chat_id = event.chat.id
    partner_name = getattr(event.chat, "first_name", None) or \
        getattr(event.chat, "title", None)
    partner_user = getattr(event.chat, "username", None)

    for msg_id in event.message_ids:
        original = find_original(chat_id, msg_id)
        if original:
            text, mtype, media_path, s_name, s_user, direction = original
            what = text or (f"[{mtype}]" if mtype else "(без текста)")
        else:
            s_name, s_user = partner_name, partner_user
            what = "(оригинал не найден в базе)"
            mtype = media_path = None

        save_row(
            tg_message_id=msg_id, chat_id=chat_id, sender_id=None,
            sender_name=s_name, sender_username=s_user, direction="in", text=what,
            media_type=mtype, media_path=media_path, event="deleted", msg_date=None,
            logged_at=now_utc(), biz_conn_id=conn_id, account_id=owner_id,
            account_name=owner_name,
        )
        log.info("🗑️ [%s] id=%s: %r", owner_name or "?", msg_id, what[:50])
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        await notify(
            bot, target,
            f"{acct_tag(owner_name)}"
            f"{who_html(s_name, s_user)} удалил(а) сообщение:\n\n"
            f"{quote(what)}\n\n"
            f"🕓 {stamp}\n"
            f"@{esc(BOT_USERNAME)}",
        )
        if mtype and media_path and os.path.exists(media_path):
            await send_saved_media(
                bot, target, mtype, media_path,
                f"↑ Медиа из удалённого сообщения ({esc(mtype)})",
            )


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
async def main() -> None:
    global BOT_USERNAME
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username or "bot"
    log.info("Бот запущен: @%s (id=%s)", me.username, me.id)
    log.info("Режим уведомлений: %s",
             f"общий чат {LOG_CHAT_ID}" if LOG_CHAT_ID
             else "в чат каждого аккаунта отдельно")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
    finally:
        DB.close()
