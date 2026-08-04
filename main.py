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

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
            source TEXT,
            added_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # 기존 DB 호환: 예전 rooms 테이블에 source 컬럼이 없으면 유지한 채 추가
    cols = [row[1] for row in cur.execute("PRAGMA table_info(rooms)").fetchall()]
    if "source" not in cols:
        cur.execute("ALTER TABLE rooms ADD COLUMN source TEXT")

    # 발송 로그 테이블: 기존 방/글 등록 데이터는 그대로 유지
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
        (
            chat_id,
            title,
            source,
            datetime.now(TIMEZONE).isoformat(),
        )
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
        """
        SELECT chat_id, title, source
        FROM rooms
        ORDER BY added_at ASC
        """
    ).fetchall()
    conn.close()
    return rows

def set_setting(key, value):
    conn = db()
    conn.execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
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

def add_send_log(chat_id, room_title, status, error="", trigger_type="자동"):
    conn = db()
    conn.execute(
        """
        INSERT INTO send_logs(sent_at, chat_id, room_title, status, error, trigger_type)
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
        title = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or str(chat_id)
        )
        return chat_id, title

    m = PUBLIC_CHAT_RE.match(url)
    if m:
        username = m.group(1)
        entity = await client.get_entity(username)
        title = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or username
        )
        return entity.id, title

    raise ValueError(
        "방 링크 형식이 아닙니다. "
        "예: https://t.me/groupname 또는 "
        "https://t.me/c/123456789/10"
    )

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

    raise ValueError(
        "게시글 링크 형식이 아닙니다. "
        "예: https://t.me/channelname/123"
    )

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

async def send_to_all_rooms(trigger_type="자동"):
    rooms = get_rooms()

    if not rooms:
        print("등록된 방이 없습니다.")
        return 0, 0

    success = 0
    failed = 0

    for room in rooms:
        while True:
            try:
                await copy_registered_post(room["chat_id"])
                add_send_log(
                    room["chat_id"], room["title"], "성공", trigger_type=trigger_type
                )
                success += 1
                print(
                    f"[발송 성공] {room['title']} "
                    f"({room['chat_id']})"
                )
                await asyncio.sleep(SEND_DELAY_SECONDS)
                break

            except FloodWaitError as e:
                wait_seconds = int(e.seconds) + 2
                print(
                    f"[FloodWait] {room['title']} - "
                    f"{wait_seconds}초 대기"
                )
                await asyncio.sleep(wait_seconds)

            except Exception as e:
                error_text = f"{type(e).__name__}: {e}"
                add_send_log(
                    room["chat_id"], room["title"], "실패", error_text, trigger_type
                )
                failed += 1
                print(
                    f"[발송 실패] {room['title']} "
                    f"({room['chat_id']}): {error_text}"
                )
                break

    return success, failed

def seconds_until_next_slot():
    now = datetime.now(TIMEZONE)
    next_run = (
        now.replace(
            minute=0,
            second=0,
            microsecond=0
        )
        + timedelta(hours=1)
    )

    if INTERVAL_HOURS > 1:
        while next_run.hour % INTERVAL_HOURS != 0:
            next_run += timedelta(hours=1)

    return max(
        1,
        (next_run - now).total_seconds()
    ), next_run

async def scheduler():
    while True:
        wait_seconds, next_run = seconds_until_next_slot()

        print(
            "다음 자동발송: "
            + next_run.strftime("%Y-%m-%d %H:%M:%S KST")
        )

        await asyncio.sleep(wait_seconds)
        await send_to_all_rooms("자동")

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/방등록(?:\s+(.+))?$"
    )
)
async def cmd_add_room(event):
    url = event.pattern_match.group(1)

    if not url:
        await event.edit(
            "❗ 사용법\n"
            "/방등록 방링크\n\n"
            "예시:\n"
            "/방등록 https://t.me/groupname\n"
            "/방등록 https://t.me/c/123456789/10"
        )
        return

    try:
        chat_id, title = await resolve_room_from_link(url)
        add_room(chat_id, title, url.strip())

        await event.edit(
            "✅ 방 등록 완료\n"
            f"{title}\n"
            f"{url.strip()}\n"
            f"ID: {chat_id}"
        )
    except Exception as e:
        await event.edit(
            "❌ 방 등록 실패\n"
            f"{type(e).__name__}: {e}"
        )

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/방삭제(?:\s+(.+))?$"
    )
)
async def cmd_remove_room(event):
    url = event.pattern_match.group(1)

    if not url:
        await event.edit(
            "❗ 사용법\n"
            "/방삭제 방링크"
        )
        return

    try:
        chat_id, title = await resolve_room_from_link(url)
        remove_room(chat_id)

        await event.edit(
            "🗑 방 삭제 완료\n"
            f"{title}"
        )
    except Exception as e:
        await event.edit(
            "❌ 방 삭제 실패\n"
            f"{type(e).__name__}: {e}"
        )

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/방목록$"
    )
)
async def cmd_room_list(event):
    rooms = get_rooms()

    if not rooms:
        await event.edit("등록된 방이 없습니다.")
        return

    lines = ["📋 자동발송 방 목록"]

    for i, room in enumerate(rooms, 1):
        source = room["source"] or str(room["chat_id"])
        lines.append(
            f"{i}. {room['title']}\n{source}"
        )

    await event.edit("\n\n".join(lines))

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/글등록(?:\s+(.+))?$"
    )
)
async def cmd_save_post(event):
    url = event.pattern_match.group(1)

    if not url:
        await event.edit(
            "❗ 사용법\n"
            "/글등록 게시글주소\n\n"
            "예시:\n"
            "/글등록 https://t.me/channelname/123"
        )
        return

    try:
        source_chat_id, source_message_id = (
            await resolve_post_from_link(url)
        )

        msg = await client.get_messages(
            int(source_chat_id),
            ids=int(source_message_id)
        )

        if not msg:
            raise RuntimeError(
                "게시글을 찾지 못했습니다."
            )

        set_setting(
            "source_chat_id",
            str(source_chat_id)
        )
        set_setting(
            "source_message_id",
            str(source_message_id)
        )
        set_setting(
            "source_post_url",
            url.strip()
        )

        await event.edit(
            "✅ 발송글 등록 완료\n"
            f"{url.strip()}"
        )

    except Exception as e:
        await event.edit(
            "❌ 글 등록 실패\n"
            f"{type(e).__name__}: {e}"
        )

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/글확인$"
    )
)
async def cmd_check_post(event):
    url = get_setting("source_post_url")

    if not url:
        await event.edit(
            "등록된 발송글이 없습니다."
        )
        return

    await event.edit(
        "📝 등록된 발송글\n"
        f"{url}"
    )

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/글삭제$"
    )
)
async def cmd_delete_post(event):
    set_setting("source_chat_id", "")
    set_setting("source_message_id", "")
    set_setting("source_post_url", "")

    await event.edit(
        "🗑 등록된 발송글을 삭제했습니다."
    )

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/발송$"
    )
)
async def cmd_send_now(event):
    await event.edit(
        "📤 등록된 방으로 테스트 발송을 시작합니다."
    )

    success, failed = await send_to_all_rooms("수동")

    await event.edit(
        f"✅ 테스트 발송 완료\n성공 {success} / 실패 {failed}"
    )

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/로그(?:\s+(\d+))?$"
    )
)
async def cmd_logs(event):
    raw_limit = event.pattern_match.group(1)
    limit = int(raw_limit) if raw_limit else 20
    limit = max(1, min(limit, 100))

    rows = get_send_logs(limit)
    if not rows:
        await event.edit("📭 발송 로그가 없습니다.")
        return

    lines = [f"📜 최근 발송 로그 {len(rows)}건"]
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["sent_at"])
            time_text = dt.astimezone(TIMEZONE).strftime("%m/%d %H:%M:%S")
        except Exception:
            time_text = str(row["sent_at"])[:19]

        mark = "✅" if row["status"] == "성공" else "❌"
        line = (
            f"{mark} {time_text} [{row['trigger_type']}]\n"
            f"{row['room_title'] or row['chat_id']}"
        )
        if row["status"] != "성공" and row["error"]:
            line += f"\n↳ {row['error'][:180]}"
        lines.append(line)

    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n… 일부 생략됨"
    await event.edit(text)

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/로그삭제$"
    )
)
async def cmd_clear_logs(event):
    clear_send_logs()
    await event.edit("🗑 발송 로그를 모두 삭제했습니다.")

@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=r"^/도움말$"
    )
)
async def cmd_help(event):
    text = (
        "📌 유저봇 명령어\n\n"
        "/방등록 방링크\n"
        "/방삭제 방링크\n"
        "/방목록\n"
        "/글등록 게시글링크\n"
        "/글확인\n"
        "/글삭제\n"
        "/발송\n"
        "/로그 - 최근 20건\n"
        "/로그 50 - 최근 50건 (최대 100)\n"
        "/로그삭제\n"
        "/도움말\n\n"
        f"자동발송 간격: {INTERVAL_HOURS}시간\n"
        f"방별 발송 대기: {SEND_DELAY_SECONDS}초\n"
        "기준: Asia/Seoul 정각"
    )

    await event.edit(text)

async def main():
    init_db()

    await client.start()
    me = await client.get_me()

    print(
        f"로그인 완료: {me.first_name} "
        f"/ id={me.id}"
    )
    print(f"DB_PATH={DB_PATH}")
    print(
        f"자동발송 간격="
        f"{INTERVAL_HOURS}시간"
    )
    print(
        f"방별 발송 대기="
        f"{SEND_DELAY_SECONDS}초"
    )

    asyncio.create_task(scheduler())
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
