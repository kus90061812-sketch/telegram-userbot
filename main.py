import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()

SESSION_STRING = (
    os.environ["SESSION_STRING"]
    .strip()
    .strip('"')
    .strip("'")
    .replace("\n", "")
    .replace("\r", "")
    .replace(" ", "")
)

INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", "1"))
DB_PATH = os.environ.get("DB_PATH", "userbot.db")
TIMEZONE = ZoneInfo("Asia/Seoul")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            added_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def add_room(chat_id, title):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO rooms(chat_id, title, added_at) VALUES (?, ?, ?)",
        (chat_id, title, datetime.now(TIMEZONE).isoformat())
    )
    conn.commit()
    conn.close()

def remove_room(chat_id):
    conn = db()
    conn.execute("DELETE FROM rooms WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def get_rooms():
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, title FROM rooms ORDER BY added_at ASC"
    ).fetchall()
    conn.close()
    return rows

def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()

def get_setting(key):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else None

async def get_chat_title(event):
    chat = await event.get_chat()
    return (
        getattr(chat, "title", None)
        or getattr(chat, "first_name", None)
        or str(event.chat_id)
    )

async def copy_registered_post(target_chat_id):
    source_chat_id = get_setting("source_chat_id")
    source_message_id = get_setting("source_message_id")

    if not source_chat_id or not source_message_id:
        raise RuntimeError("등록된 글이 없습니다.")

    msg = await client.get_messages(int(source_chat_id), ids=int(source_message_id))
    if not msg:
        raise RuntimeError("등록된 원본 글을 찾지 못했습니다.")

    await client.send_message(target_chat_id, msg)

async def send_to_all_rooms():
    rooms = get_rooms()
    if not rooms:
        print("등록된 방이 없습니다.")
        return

    for room in rooms:
        try:
            await copy_registered_post(room["chat_id"])
            print(f"[발송 성공] {room['title']} ({room['chat_id']})")
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"[발송 실패] {room['title']} ({room['chat_id']}): {type(e).__name__}: {e}")

def seconds_until_next_slot():
    now = datetime.now(TIMEZONE)
    next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    if INTERVAL_HOURS > 1:
        while next_run.hour % INTERVAL_HOURS != 0:
            next_run += timedelta(hours=1)

    return max(1, (next_run - now).total_seconds()), next_run

async def scheduler():
    while True:
        wait_seconds, next_run = seconds_until_next_slot()
        print(f"다음 자동발송: {next_run.strftime('%Y-%m-%d %H:%M:%S KST')}")
        await asyncio.sleep(wait_seconds)
        await send_to_all_rooms()

@client.on(events.NewMessage(outgoing=True, pattern=r"^/방등록$"))
async def cmd_add_room(event):
    title = await get_chat_title(event)
    add_room(event.chat_id, title)
    await event.edit(f"✅ 방 등록 완료\n{title}\nID: {event.chat_id}")

@client.on(events.NewMessage(outgoing=True, pattern=r"^/방삭제$"))
async def cmd_remove_room(event):
    remove_room(event.chat_id)
    await event.edit("🗑 현재 방을 자동발송 목록에서 삭제했습니다.")

@client.on(events.NewMessage(outgoing=True, pattern=r"^/방목록$"))
async def cmd_room_list(event):
    rooms = get_rooms()
    if not rooms:
        await event.edit("등록된 방이 없습니다.")
        return

    lines = ["📋 자동발송 방 목록"]
    for i, room in enumerate(rooms, 1):
        lines.append(f"{i}. {room['title']} ({room['chat_id']})")

    await event.edit("\n".join(lines))

@client.on(events.NewMessage(outgoing=True, pattern=r"^/글등록$"))
async def cmd_save_post(event):
    if not event.is_reply:
        await event.edit("❗ 등록할 게시글에 답장으로 /글등록 을 입력하세요.")
        return

    replied = await event.get_reply_message()
    if not replied:
        await event.edit("원본 게시글을 찾지 못했습니다.")
        return

    set_setting("source_chat_id", str(event.chat_id))
    set_setting("source_message_id", str(replied.id))

    await event.edit(
        f"✅ 발송글 등록 완료\n"
        f"채팅 ID: {event.chat_id}\n"
        f"메시지 ID: {replied.id}"
    )

@client.on(events.NewMessage(outgoing=True, pattern=r"^/글확인$"))
async def cmd_check_post(event):
    source_chat_id = get_setting("source_chat_id")
    source_message_id = get_setting("source_message_id")

    if not source_chat_id or not source_message_id:
        await event.edit("등록된 발송글이 없습니다.")
        return

    await event.edit(
        f"📝 등록된 발송글\n"
        f"채팅 ID: {source_chat_id}\n"
        f"메시지 ID: {source_message_id}"
    )

@client.on(events.NewMessage(outgoing=True, pattern=r"^/발송$"))
async def cmd_send_now(event):
    await event.edit("📤 등록된 방으로 테스트 발송을 시작합니다.")
    await send_to_all_rooms()
    await event.edit("✅ 테스트 발송 완료")

@client.on(events.NewMessage(outgoing=True, pattern=r"^/도움말$"))
async def cmd_help(event):
    text = (
        "📌 유저봇 명령어\n\n"
        "/방등록 - 현재 방을 자동발송 대상으로 등록\n"
        "/방삭제 - 현재 방 삭제\n"
        "/방목록 - 등록된 방 확인\n"
        "/글등록 - 답장한 게시글을 발송글로 등록\n"
        "/글확인 - 등록된 글 정보 확인\n"
        "/발송 - 지금 즉시 전체 방으로 테스트 발송\n"
        "/도움말 - 명령어 보기\n\n"
        f"자동발송 간격: {INTERVAL_HOURS}시간\n"
        "기준: Asia/Seoul 정각"
    )
    await event.edit(text)

async def main():
    init_db()
    await client.start()
    me = await client.get_me()

    print(f"로그인 완료: {me.first_name} / id={me.id}")
    print(f"DB_PATH={DB_PATH}")
    print(f"자동발송 간격={INTERVAL_HOURS}시간")

    asyncio.create_task(scheduler())
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
