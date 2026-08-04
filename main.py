import os
import re
import asyncio
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

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
SEND_DELAY_SECONDS = float(os.environ.get("SEND_DELAY_SECONDS", "2.0"))
DB_PATH = os.environ.get("DB_PATH", "userbot.db")
TIMEZONE = ZoneInfo("Asia/Seoul")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

PUBLIC_POST_RE = re.compile(
    r"^(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)(?:\?.*)?$"
)
PRIVATE_POST_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(\d+)/(\d+)(?:\?.*)?$"
)
PUBLIC_CHAT_RE = re.compile(
    r"^(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:/)?(?:\?.*)?$"
)
PRIVATE_CHAT_FROM_POST_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?(?:\?.*)?$"
)

MY_ID = None

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)

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
            source TEXT,
            added_at TEXT NOT NULL
        )
    """)

    if not column_exists(conn, "rooms", "source"):
        cur.execute("ALTER TABLE rooms ADD COLUMN source TEXT")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS send_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL,
            chat_id INTEGER,
            room_title TEXT,
            status TEXT NOT NULL,
            error TEXT,
            trigger_type TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

def add_room(chat_id, title, source):
    conn = db()
    conn.execute(
        """
        INSERT OR REPLACE INTO rooms(chat_id, title, source, added_at)
        VALUES (?, ?, ?, ?)
        """,
        (chat_id, title, source, datetime.now(TIMEZONE).isoformat())
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
        "SELECT chat_id, title, source FROM rooms ORDER BY added_at ASC"
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

def add_send_log(chat_id, room_title, status, error="", trigger_type="auto"):
    conn = db()
    conn.execute(
        """
        INSERT INTO send_logs(
            sent_at, chat_id, room_title, status, error, trigger_type
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(TIMEZONE).isoformat(),
            chat_id,
            room_title,
            status,
            (error or "")[:1000],
            trigger_type,
        )
    )
    conn.commit()
    conn.close()

def get_send_logs(limit=20):
    limit = max(1, min(int(limit), 100))
    conn = db()
    rows = conn.execute(
        """
        SELECT id, sent_at, chat_id, room_title, status, error, trigger_type
        FROM send_logs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,)
    ).fetchall()
    conn.close()
    return rows

def clear_send_logs():
    conn = db()
    conn.execute("DELETE FROM send_logs")
    conn.commit()
    conn.close()

async def resolve_room_from_link(url: str):
    url = url.strip()

    m = PRIVATE_CHAT_FROM_POST_RE.match(url)
    if m:
        internal_id = int(m.group(1))
        chat_id = int(f"-100{internal_id}")
        entity = await client.get_entity(chat_id)
        title = getattr(entity, "title", None) or str(chat_id)
        return chat_id, title

    m = PUBLIC_CHAT_RE.match(url)
    if m:
        username = m.group(1)
        entity = await client.get_entity(username)
        title = getattr(entity, "title", None) or username
        return entity.id, title

    raise ValueError("방 링크 형식이 아닙니다.")

async def resolve_post_from_link(url: str):
    url = url.strip()

    m = PRIVATE_POST_RE.match(url)
    if m:
        internal_id = int(m.group(1))
        message_id = int(m.group(2))
        chat_id = int(f"-100{internal_id}")
        return chat_id, message_id

    m = PUBLIC_POST_RE.match(url)
    if m:
        username = m.group(1)
        message_id = int(m.group(2))
        entity = await client.get_entity(username)
        return entity.id, message_id

    raise ValueError("게시글 링크 형식이 아닙니다.")

async def get_registered_message():
    source_chat_id = get_setting("source_chat_id")
    source_message_id = get_setting("source_message_id")

    if not source_chat_id or not source_message_id:
        raise RuntimeError("등록된 게시글이 없습니다.")

    msg = await client.get_messages(
        int(source_chat_id),
        ids=int(source_message_id)
    )

    if not msg:
        raise RuntimeError("등록된 원본 게시글을 찾지 못했습니다.")

    return msg

async def copy_registered_post(target_chat_id):
    msg = await get_registered_message()
    await client.send_message(target_chat_id, msg)

async def send_to_all_rooms(trigger_type="auto"):
    rooms = get_rooms()
    if not rooms:
        return 0, 0

    success = 0
    failed = 0

    for room in rooms:
        while True:
            try:
                await copy_registered_post(room["chat_id"])
                add_send_log(
                    room["chat_id"],
                    room["title"],
                    "성공",
                    trigger_type=trigger_type
                )
                success += 1
                await asyncio.sleep(SEND_DELAY_SECONDS)
                break

            except FloodWaitError as e:
                await asyncio.sleep(int(e.seconds) + 2)

            except Exception as e:
                add_send_log(
                    room["chat_id"],
                    room["title"],
                    "실패",
                    f"{type(e).__name__}: {e}",
                    trigger_type
                )
                failed += 1
                break

    return success, failed

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
        print("다음 자동발송:", next_run.strftime("%Y-%m-%d %H:%M:%S KST"))
        await asyncio.sleep(wait_seconds)
        await send_to_all_rooms("자동")

async def respond(event, text):
    try:
        await event.edit(text)
    except Exception:
        await event.respond(text)

async def handle_command(event):
    global MY_ID

    if event.sender_id != MY_ID:
        return

    text = (event.raw_text or "").strip()
    if not text.startswith("/"):
        return

    print(f"[명령감지] {text}")

    if text == "/도움말":
        await respond(
            event,
            "📌 유저봇 명령어\n\n"
            "/방등록 방링크\n"
            "/방삭제 방링크\n"
            "/방목록\n"
            "/글등록 게시글링크\n"
            "/글확인\n"
            "/글삭제\n"
            "/발송\n"
            "/로그\n"
            "/로그 50\n"
            "/로그삭제\n"
            "/도움말"
        )
        return

    if text == "/방목록":
        rooms = get_rooms()
        if not rooms:
            await respond(event, "등록된 방이 없습니다.")
            return
        lines = ["📋 자동발송 방 목록"]
        for i, room in enumerate(rooms, 1):
            lines.append(f"{i}. {room['title']}\n{room['source'] or room['chat_id']}")
        await respond(event, "\n\n".join(lines)[:4000])
        return

    if text.startswith("/방등록 "):
        url = text.split(" ", 1)[1].strip()
        try:
            chat_id, title = await resolve_room_from_link(url)
            add_room(chat_id, title, url)
            await respond(event, f"✅ 방 등록 완료\n{title}\n{url}")
        except Exception as e:
            await respond(event, f"❌ 방 등록 실패\n{type(e).__name__}: {e}")
        return

    if text.startswith("/방삭제 "):
        url = text.split(" ", 1)[1].strip()
        try:
            chat_id, title = await resolve_room_from_link(url)
            remove_room(chat_id)
            await respond(event, f"🗑 방 삭제 완료\n{title}")
        except Exception as e:
            await respond(event, f"❌ 방 삭제 실패\n{type(e).__name__}: {e}")
        return

    if text.startswith("/글등록 "):
        url = text.split(" ", 1)[1].strip()
        try:
            source_chat_id, source_message_id = await resolve_post_from_link(url)
            msg = await client.get_messages(int(source_chat_id), ids=int(source_message_id))
            if not msg:
                raise RuntimeError("게시글을 찾지 못했습니다.")

            set_setting("source_chat_id", str(source_chat_id))
            set_setting("source_message_id", str(source_message_id))
            set_setting("source_post_url", url)
            await respond(event, f"✅ 발송글 등록 완료\n{url}")
        except Exception as e:
            await respond(event, f"❌ 글 등록 실패\n{type(e).__name__}: {e}")
        return

    if text == "/글확인":
        url = get_setting("source_post_url")
        await respond(
            event,
            f"📝 등록된 발송글\n{url}" if url else "등록된 발송글이 없습니다."
        )
        return

    if text == "/글삭제":
        set_setting("source_chat_id", "")
        set_setting("source_message_id", "")
        set_setting("source_post_url", "")
        await respond(event, "🗑 등록된 발송글을 삭제했습니다.")
        return

    if text == "/발송":
        await respond(event, "📤 테스트 발송 시작")
        success, failed = await send_to_all_rooms("수동")
        await respond(event, f"✅ 테스트 발송 완료\n성공 {success} / 실패 {failed}")
        return

    if text.startswith("/로그"):
        parts = text.split()
        limit = 20
        if len(parts) >= 2 and parts[1].isdigit():
            limit = max(1, min(int(parts[1]), 100))

        rows = get_send_logs(limit)
        if not rows:
            await respond(event, "📭 발송 로그가 없습니다.")
            return

        lines = [f"📜 최근 발송 로그 {len(rows)}건"]
        for row in rows:
            mark = "✅" if row["status"] == "성공" else "❌"
            try:
                dt = datetime.fromisoformat(row["sent_at"]).astimezone(TIMEZONE)
                ts = dt.strftime("%m/%d %H:%M:%S")
            except Exception:
                ts = row["sent_at"][:19]

            item = (
                f"{mark} {ts} [{row['trigger_type']}]\n"
                f"{row['room_title'] or row['chat_id']}"
            )
            if row["status"] != "성공" and row["error"]:
                item += f"\n↳ {row['error'][:180]}"
            lines.append(item)

        await respond(event, "\n\n".join(lines)[:4000])
        return

    if text == "/로그삭제":
        clear_send_logs()
        await respond(event, "🗑 발송 로그를 모두 삭제했습니다.")
        return

@client.on(events.NewMessage())
async def catch_all_messages(event):
    await handle_command(event)

async def main():
    global MY_ID

    init_db()
    await client.start()

    me = await client.get_me()
    MY_ID = me.id

    print(f"로그인 완료: {me.first_name} / id={MY_ID}")
    print(f"DB_PATH={DB_PATH}")
    print(f"자동발송 간격={INTERVAL_HOURS}시간")
    print(f"방별 발송 대기={SEND_DELAY_SECONDS}초")
    print("명령어 감지기 활성화")

    asyncio.create_task(scheduler())
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
