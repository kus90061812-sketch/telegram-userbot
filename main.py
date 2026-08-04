import os
import re
import time
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SlowModeWaitError


# =========================
# 환경설정
# =========================
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

DATABASE_URL = os.environ["DATABASE_URL"].strip()

# 자동발송 주기(시간)
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", "1"))

# 방 하나 발송 후 다음 방까지 쉬는 시간(초)
# Railway Variables에서 SEND_DELAY_SECONDS로 조절 가능
SEND_DELAY_SECONDS = float(os.environ.get("SEND_DELAY_SECONDS", "5.0"))

TIMEZONE = ZoneInfo("Asia/Seoul")

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)

# 공개 게시글: https://t.me/channelname/123
PUBLIC_POST_RE = re.compile(
    r"^(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)(?:\?.*)?$"
)

# 비공개 게시글: https://t.me/c/123456789/123
PRIVATE_POST_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(\d+)/(\d+)(?:\?.*)?$"
)

# 공개 방: https://t.me/groupname
PUBLIC_CHAT_RE = re.compile(
    r"^(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:/)?(?:\?.*)?$"
)

# 비공개 방 메시지 링크: https://t.me/c/123456789/10
PRIVATE_CHAT_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?(?:\?.*)?$"
)


# =========================
# 런타임 상태
# =========================
MY_ID = None

# 슬로우모드 방별 재시도 작업
RETRY_TASKS = {}

# 계정 전체 FloodWait 만료 시각(monotonic)
GLOBAL_FLOOD_UNTIL = 0.0

# 자동/수동 전체발송 동시 실행 방지
SEND_BATCH_LOCK = asyncio.Lock()


# =========================
# PostgreSQL
# =========================
def db():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    )


def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    chat_id BIGINT PRIMARY KEY,
                    title TEXT,
                    source TEXT,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS send_logs (
                    id BIGSERIAL PRIMARY KEY,
                    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    chat_id BIGINT,
                    room_title TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    trigger_type TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_send_logs_id
                ON send_logs(id DESC)
            """)


def add_room(chat_id, title, source):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rooms(
                    chat_id,
                    title,
                    source,
                    added_at
                )
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    source = EXCLUDED.source,
                    added_at = NOW()
            """, (chat_id, title, source))


def remove_room(chat_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rooms WHERE chat_id = %s",
                (chat_id,)
            )


def get_rooms():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id, title, source
                FROM rooms
                ORDER BY added_at ASC
            """)
            return cur.fetchall()


def set_setting(key, value):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value
            """, (key, value))


def get_setting(key):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM settings WHERE key = %s",
                (key,)
            )
            row = cur.fetchone()
            return row["value"] if row else None


def add_send_log(
    chat_id,
    room_title,
    status,
    error="",
    trigger_type="자동"
):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO send_logs(
                    sent_at,
                    chat_id,
                    room_title,
                    status,
                    error,
                    trigger_type
                )
                VALUES (NOW(), %s, %s, %s, %s, %s)
            """, (
                chat_id,
                room_title,
                status,
                (error or "")[:1000],
                trigger_type,
            ))


def get_send_logs(limit=20):
    limit = max(1, min(int(limit), 100))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    sent_at,
                    chat_id,
                    room_title,
                    status,
                    error,
                    trigger_type
                FROM send_logs
                ORDER BY id DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


def clear_send_logs():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM send_logs")


# =========================
# 링크 / 방 해석
# =========================
async def resolve_room_from_link(value: str):
    value = value.strip()

    # 숫자 채팅 ID 직접 등록
    # 예: /방등록 -1001935285339
    if re.fullmatch(r"-100\d+", value):
        chat_id = int(value)
        entity = await client.get_entity(chat_id)

        title = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or str(chat_id)
        )
        return chat_id, title

    # 비공개 방 메시지 링크
    m = PRIVATE_CHAT_RE.match(value)
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

    # 공개 방 링크
    m = PUBLIC_CHAT_RE.match(value)
    if m:
        username = m.group(1)

        entity = await client.get_entity(username)
        chat_id = utils.get_peer_id(entity)

        title = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or username
        )
        return chat_id, title

    raise ValueError(
        "방 링크/ID 형식이 아닙니다. "
        "예: -1001234567890 / "
        "https://t.me/groupname / "
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
        chat_id = utils.get_peer_id(entity)

        return chat_id, message_id

    raise ValueError(
        "게시글 링크 형식이 아닙니다. "
        "예: https://t.me/channelname/123"
    )


# =========================
# 원본 게시글 / 전달
# =========================
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


async def forward_registered_post(target_chat_id):
    """
    원본 채널 게시글을 '새 메시지 복사'가 아니라
    Telegram 전달하기(Forward) 방식으로 보낸다.
    """
    msg = await get_registered_message()

    await client.forward_messages(
        entity=target_chat_id,
        messages=msg,
    )


# =========================
# FloodWait / SlowMode
# =========================
def set_global_flood_wait(seconds):
    global GLOBAL_FLOOD_UNTIL

    wait_seconds = max(1, int(seconds) + 2)

    GLOBAL_FLOOD_UNTIL = max(
        GLOBAL_FLOOD_UNTIL,
        time.monotonic() + wait_seconds
    )

    return wait_seconds


def get_global_flood_remaining():
    return max(
        0,
        int(GLOBAL_FLOOD_UNTIL - time.monotonic())
    )


async def wait_for_global_flood():
    """
    계정 전체 FloodWait가 있으면 기다린다.
    60초 단위로 깨어나 로그를 남겨서 살아있는지 확인 가능.
    """
    while True:
        remaining = get_global_flood_remaining()

        if remaining <= 0:
            return

        print(
            f"[계정 FloodWait 대기] "
            f"{remaining}초 남음"
        )

        await asyncio.sleep(
            min(remaining + 1, 60)
        )


async def retry_room_after_slowmode(
    room,
    wait_seconds,
    trigger_type
):
    """
    방 자체 SlowMode만 별도 재시도.
    계정 전체 FloodWait는 전역 대기 처리.
    """
    chat_id = room["chat_id"]
    title = room["title"]

    remaining = max(
        1,
        int(wait_seconds) + 2
    )

    try:
        while True:
            print(
                f"[슬로우모드 재시도 대기] "
                f"{title} - {remaining}초"
            )

            await asyncio.sleep(remaining)
            await wait_for_global_flood()

            try:
                await forward_registered_post(chat_id)

                add_send_log(
                    chat_id,
                    title,
                    "성공",
                    trigger_type=f"{trigger_type}-슬로우재시도"
                )

                print(
                    f"[슬로우모드 재시도 성공] "
                    f"{title} ({chat_id})"
                )
                return

            except SlowModeWaitError as e:
                remaining = max(
                    1,
                    int(e.seconds) + 2
                )

                print(
                    f"[슬로우모드 재연장] "
                    f"{title} - {remaining}초"
                )

            except FloodWaitError as e:
                wait = set_global_flood_wait(e.seconds)

                print(
                    f"[계정 FloodWait] "
                    f"{wait}초 대기 후 "
                    f"{title} 재시도"
                )

                await wait_for_global_flood()
                remaining = 1

            except Exception as e:
                error_text = (
                    f"{type(e).__name__}: {e}"
                )

                add_send_log(
                    chat_id,
                    title,
                    "실패",
                    error_text,
                    f"{trigger_type}-슬로우재시도"
                )

                print(
                    f"[슬로우모드 재시도 실패] "
                    f"{title} ({chat_id}): "
                    f"{error_text}"
                )
                return

    finally:
        RETRY_TASKS.pop(
            chat_id,
            None
        )


def schedule_slowmode_retry(
    room,
    wait_seconds,
    trigger_type
):
    chat_id = room["chat_id"]

    old_task = RETRY_TASKS.get(chat_id)

    if old_task and not old_task.done():
        print(
            f"[슬로우모드 재시도 중복 방지] "
            f"{room['title']}"
        )
        return False

    RETRY_TASKS[chat_id] = asyncio.create_task(
        retry_room_after_slowmode(
            room,
            wait_seconds,
            trigger_type
        )
    )

    return True


async def send_one_room(
    room,
    trigger_type
):
    """
    한 방 발송.
    - SlowMode: 그 방만 별도 재시도
    - FloodWait: 계정 전체 제한이므로 기다렸다가 현재 방부터 재개
    """
    chat_id = room["chat_id"]
    title = room["title"]

    while True:
        await wait_for_global_flood()

        try:
            await forward_registered_post(chat_id)

            add_send_log(
                chat_id,
                title,
                "성공",
                trigger_type=trigger_type
            )

            print(
                f"[발송 성공] "
                f"{title} ({chat_id})"
            )

            return "success"

        # SlowModeWaitError는 FloodWaitError 계열일 수 있으므로
        # 반드시 FloodWaitError보다 먼저 처리
        except SlowModeWaitError as e:
            wait_seconds = max(
                1,
                int(e.seconds) + 2
            )

            schedule_slowmode_retry(
                room,
                wait_seconds,
                trigger_type
            )

            print(
                f"[슬로우모드 재시도 예약] "
                f"{title} - "
                f"{wait_seconds}초 후"
            )

            return "retry"

        except FloodWaitError as e:
            wait_seconds = set_global_flood_wait(
                e.seconds
            )

            print(
                f"[계정 FloodWait] "
                f"{wait_seconds}초 대기 후 "
                f"현재 방부터 이어서 진행"
            )

            # 모든 방을 재시도로 넣지 않는다.
            # 계정 전체 제한이 풀릴 때까지 기다린 뒤
            # 현재 방을 다시 시도한다.
            await wait_for_global_flood()

        except Exception as e:
            error_text = (
                f"{type(e).__name__}: {e}"
            )

            add_send_log(
                chat_id,
                title,
                "실패",
                error_text,
                trigger_type
            )

            print(
                f"[발송 실패] "
                f"{title} ({chat_id}): "
                f"{error_text}"
            )

            return "failed"


async def send_to_all_rooms(
    trigger_type="자동"
):
    rooms = get_rooms()

    if not rooms:
        print("등록된 방이 없습니다.")
        return 0, 0, 0

    success = 0
    failed = 0
    retry = 0

    # 자동발송과 /발송 동시 실행 금지
    async with SEND_BATCH_LOCK:
        for room in rooms:
            chat_id = room["chat_id"]

            # 이 방은 이미 SlowMode 재시도 대기 중
            task = RETRY_TASKS.get(chat_id)

            if task and not task.done():
                print(
                    f"[슬로우모드 대기중 건너뜀] "
                    f"{room['title']} ({chat_id})"
                )

                retry += 1
                continue

            result = await send_one_room(
                room,
                trigger_type
            )

            if result == "success":
                success += 1

                # 성공한 방 다음에만 발송 간격 적용
                await asyncio.sleep(
                    SEND_DELAY_SECONDS
                )

            elif result == "retry":
                retry += 1

            else:
                failed += 1

    return success, failed, retry


# =========================
# 스케줄러
# =========================
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
        while (
            next_run.hour
            % INTERVAL_HOURS
            != 0
        ):
            next_run += timedelta(hours=1)

    return (
        max(
            1,
            (next_run - now).total_seconds()
        ),
        next_run
    )


async def scheduler():
    while True:
        wait_seconds, next_run = (
            seconds_until_next_slot()
        )

        print(
            "다음 자동발송:",
            next_run.strftime(
                "%Y-%m-%d %H:%M:%S KST"
            )
        )

        await asyncio.sleep(wait_seconds)

        success, failed, retry = (
            await send_to_all_rooms("자동")
        )

        print(
            f"[자동발송 완료] "
            f"성공={success} "
            f"실패={failed} "
            f"슬로우재시도={retry}"
        )


# =========================
# 명령어
# =========================
async def respond(event, text):
    try:
        await event.edit(text)
    except Exception:
        await event.respond(text)


async def handle_command(event):
    text = (
        event.raw_text
        or ""
    ).strip()

    if not text.startswith("/"):
        return

    print(
        f"[명령감지] {text}"
    )

    if text == "/도움말":
        await respond(
            event,
            "📌 유저봇 명령어\n\n"
            "/방등록 방링크 또는 -100채팅ID\n"
            "/방삭제 방링크 또는 -100채팅ID\n"
            "/방목록\n"
            "/글등록 게시글링크\n"
            "/글확인\n"
            "/글삭제\n"
            "/발송\n"
            "/로그\n"
            "/로그 50\n"
            "/로그삭제\n"
            "/상태\n"
            "/재시도현황\n"
            "/도움말"
        )
        return

    if text in (
        "/상태",
        "/재시도현황"
    ):
        active_ids = [
            chat_id
            for chat_id, task
            in RETRY_TASKS.items()
            if not task.done()
        ]

        flood_remaining = (
            get_global_flood_remaining()
        )

        lines = [
            "📊 유저봇 상태",
            (
                "전체발송 진행중: "
                + (
                    "예"
                    if SEND_BATCH_LOCK.locked()
                    else "아니오"
                )
            ),
            (
                "계정 FloodWait 남은 시간: "
                f"{flood_remaining}초"
            ),
            (
                "슬로우모드 재시도 방: "
                f"{len(active_ids)}개"
            ),
            (
                "방별 발송 간격: "
                f"{SEND_DELAY_SECONDS}초"
            ),
            "발송 방식: 채널 게시글 전달(Forward)",
        ]

        if active_ids:
            lines.append(
                "\n재시도 대기 ID\n"
                + "\n".join(
                    str(x)
                    for x in active_ids[:30]
                )
            )

        await respond(
            event,
            "\n".join(lines)
        )
        return

    if text == "/방목록":
        rooms = get_rooms()

        if not rooms:
            await respond(
                event,
                "등록된 방이 없습니다."
            )
            return

        lines = [
            "📋 자동발송 방 목록"
        ]

        for i, room in enumerate(
            rooms,
            1
        ):
            source = (
                room["source"]
                or str(room["chat_id"])
            )

            lines.append(
                f"{i}. {room['title']}\n"
                f"{source}"
            )

        await respond(
            event,
            "\n\n".join(lines)[:4000]
        )
        return

    if text.startswith(
        "/방등록 "
    ):
        value = text.split(
            " ",
            1
        )[1].strip()

        try:
            chat_id, title = (
                await resolve_room_from_link(
                    value
                )
            )

            add_room(
                chat_id,
                title,
                value
            )

            await respond(
                event,
                f"✅ 방 등록 완료\n"
                f"{title}\n"
                f"{value}\n"
                f"ID: {chat_id}"
            )

        except Exception as e:
            await respond(
                event,
                f"❌ 방 등록 실패\n"
                f"{type(e).__name__}: {e}"
            )

        return

    if text.startswith(
        "/방삭제 "
    ):
        value = text.split(
            " ",
            1
        )[1].strip()

        try:
            chat_id, title = (
                await resolve_room_from_link(
                    value
                )
            )

            remove_room(chat_id)

            task = RETRY_TASKS.pop(
                chat_id,
                None
            )

            if task and not task.done():
                task.cancel()

            await respond(
                event,
                f"🗑 방 삭제 완료\n"
                f"{title}"
            )

        except Exception as e:
            await respond(
                event,
                f"❌ 방 삭제 실패\n"
                f"{type(e).__name__}: {e}"
            )

        return

    if text.startswith(
        "/글등록 "
    ):
        url = text.split(
            " ",
            1
        )[1].strip()

        try:
            (
                source_chat_id,
                source_message_id
            ) = await resolve_post_from_link(
                url
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
                url
            )

            await respond(
                event,
                f"✅ 전달할 게시글 등록 완료\n"
                f"{url}"
            )

        except Exception as e:
            await respond(
                event,
                f"❌ 글 등록 실패\n"
                f"{type(e).__name__}: {e}"
            )

        return

    if text == "/글확인":
        url = get_setting(
            "source_post_url"
        )

        await respond(
            event,
            (
                f"📝 등록된 전달 게시글\n"
                f"{url}"
            )
            if url
            else (
                "등록된 발송글이 없습니다."
            )
        )
        return

    if text == "/글삭제":
        set_setting(
            "source_chat_id",
            ""
        )
        set_setting(
            "source_message_id",
            ""
        )
        set_setting(
            "source_post_url",
            ""
        )

        await respond(
            event,
            "🗑 등록된 발송글을 삭제했습니다."
        )
        return

    if text == "/발송":
        if SEND_BATCH_LOCK.locked():
            await respond(
                event,
                "⏳ 이미 전체 발송이 진행 중입니다.\n"
                "/상태 로 확인하세요."
            )
            return

        await respond(
            event,
            "📤 전달 발송 시작"
        )

        success, failed, retry = (
            await send_to_all_rooms("수동")
        )

        await respond(
            event,
            "✅ 전달 발송 완료\n"
            f"성공 {success} / "
            f"실패 {failed} / "
            f"슬로우재시도 {retry}"
        )
        return

    if text == "/로그삭제":
        clear_send_logs()

        await respond(
            event,
            "🗑 발송 로그를 모두 삭제했습니다."
        )
        return

    if (
        text == "/로그"
        or text.startswith(
            "/로그 "
        )
    ):
        parts = text.split()
        limit = 20

        if (
            len(parts) >= 2
            and parts[1].isdigit()
        ):
            limit = max(
                1,
                min(
                    int(parts[1]),
                    100
                )
            )

        rows = get_send_logs(limit)

        if not rows:
            await respond(
                event,
                "📭 발송 로그가 없습니다."
            )
            return

        lines = [
            f"📜 최근 발송 로그 "
            f"{len(rows)}건"
        ]

        for row in rows:
            mark = (
                "✅"
                if row["status"] == "성공"
                else "❌"
            )

            sent_at = row["sent_at"]

            if sent_at.tzinfo is None:
                sent_at = (
                    sent_at.replace(
                        tzinfo=TIMEZONE
                    )
                )

            ts = (
                sent_at
                .astimezone(TIMEZONE)
                .strftime(
                    "%m/%d %H:%M:%S"
                )
            )

            item = (
                f"{mark} {ts} "
                f"[{row['trigger_type']}]\n"
                f"{row['room_title'] or row['chat_id']}"
            )

            if (
                row["status"] != "성공"
                and row["error"]
            ):
                item += (
                    f"\n↳ "
                    f"{row['error'][:180]}"
                )

            lines.append(item)

        await respond(
            event,
            "\n\n".join(lines)[:4000]
        )
        return


# 내 계정이 직접 보낸 명령만 처리
@client.on(
    events.NewMessage(
        outgoing=True
    )
)
async def catch_outgoing_commands(
    event
):
    try:
        await handle_command(event)
    except Exception as e:
        print(
            f"[명령 처리 오류] "
            f"{type(e).__name__}: {e}"
        )


# =========================
# 실행
# =========================
async def main():
    global MY_ID

    init_db()
    print("PostgreSQL 연결 완료")

    await client.start()

    me = await client.get_me()
    MY_ID = me.id

    print(
        f"로그인 완료: "
        f"{me.first_name} / id={MY_ID}"
    )
    print(
        f"자동발송 간격="
        f"{INTERVAL_HOURS}시간"
    )
    print(
        f"방별 발송 대기="
        f"{SEND_DELAY_SECONDS}초"
    )
    print(
        "명령어 감지기 활성화"
        "(outgoing=True)"
    )
    print(
        "발송 방식="
        "채널 게시글 전달(Forward)"
    )
    print(
        "SlowMode/FloodWait "
        "분리 처리 활성화"
    )

    asyncio.create_task(
        scheduler()
    )

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
