import os
import re
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
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", "1"))

# Railway Variables에서 조절
SEND_DELAY_SECONDS = float(os.environ.get("SEND_DELAY_SECONDS", "30.0"))

# 재시도 확인 주기
RETRY_CHECK_SECONDS = int(os.environ.get("RETRY_CHECK_SECONDS", "30"))

# 레이트리밋 설정
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
BATCH_PAUSE_SECONDS = float(os.environ.get("BATCH_PAUSE_SECONDS", "300"))

TIMEZONE = ZoneInfo("Asia/Seoul")

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)


# =========================
# 링크 패턴
# =========================
PUBLIC_POST_RE = re.compile(
    r"^(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)(?:\?.*)?$"
)
PRIVATE_POST_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(\d+)/(\d+)(?:\?.*)?$"
)
PUBLIC_CHAT_RE = re.compile(
    r"^(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:/)?(?:\?.*)?$"
)
PRIVATE_CHAT_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?(?:\?.*)?$"
)


# =========================
# 런타임 상태
# =========================
MY_ID = None

# 자동발송과 수동 /발송 동시 실행 방지
SEND_BATCH_LOCK = asyncio.Lock()

# 재시도 worker가 같은 방을 동시에 집지 않게 방지
RETRY_IN_PROGRESS = set()


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

            # 방별 FloodWait / SlowMode 재시도 상태
            cur.execute("""
                CREATE TABLE IF NOT EXISTS room_retries (
                    chat_id BIGINT PRIMARY KEY,
                    room_title TEXT,
                    retry_at TIMESTAMPTZ NOT NULL,
                    reason TEXT,
                    trigger_type TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_room_retries_retry_at
                ON room_retries(retry_at ASC)
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
            cur.execute(
                "DELETE FROM room_retries WHERE chat_id = %s",
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


def get_room(chat_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id, title, source
                FROM rooms
                WHERE chat_id = %s
            """, (chat_id,))
            return cur.fetchone()


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


def set_room_retry(
    chat_id,
    room_title,
    wait_seconds,
    reason,
    trigger_type
):
    wait_seconds = max(1, int(wait_seconds) + 2)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO room_retries(
                    chat_id,
                    room_title,
                    retry_at,
                    reason,
                    trigger_type,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    NOW() + (%s * INTERVAL '1 second'),
                    %s,
                    %s,
                    NOW()
                )
                ON CONFLICT (chat_id) DO UPDATE SET
                    room_title = EXCLUDED.room_title,
                    retry_at = EXCLUDED.retry_at,
                    reason = EXCLUDED.reason,
                    trigger_type = EXCLUDED.trigger_type,
                    updated_at = NOW()
            """, (
                chat_id,
                room_title,
                wait_seconds,
                reason,
                trigger_type,
            ))

    return wait_seconds


def clear_room_retry(chat_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM room_retries WHERE chat_id = %s",
                (chat_id,)
            )


def get_room_retry(chat_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    chat_id,
                    room_title,
                    retry_at,
                    reason,
                    trigger_type
                FROM room_retries
                WHERE chat_id = %s
            """, (chat_id,))
            return cur.fetchone()


def get_due_retries(limit=20):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    chat_id,
                    room_title,
                    retry_at,
                    reason,
                    trigger_type
                FROM room_retries
                WHERE retry_at <= NOW()
                ORDER BY retry_at ASC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


def get_retry_count():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM room_retries"
            )
            row = cur.fetchone()
            return int(row["cnt"])


def get_retry_list(limit=30):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    chat_id,
                    room_title,
                    retry_at,
                    reason
                FROM room_retries
                ORDER BY retry_at ASC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


# =========================
# 링크 / 방 해석
# =========================
async def resolve_room_from_link(value: str):
    value = value.strip()

    if re.fullmatch(r"-100\d+", value):
        chat_id = int(value)
        entity = await client.get_entity(chat_id)

        title = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or str(chat_id)
        )
        return chat_id, title

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
    msg = await get_registered_message()

    await client.forward_messages(
        entity=target_chat_id,
        messages=msg,
    )


# =========================
# 방별 발송
# =========================
async def send_one_room(room, trigger_type):
    chat_id = room["chat_id"]
    title = room["title"]

    # 이미 대기중이면 이 방만 건너뜀
    retry_state = get_room_retry(chat_id)

    if retry_state:
        retry_at = retry_state["retry_at"]

        if retry_at > datetime.now(TIMEZONE):
            print(
                f"[대기중 건너뜀] "
                f"{title} ({chat_id}) - "
                f"{retry_at.astimezone(TIMEZONE).strftime('%m/%d %H:%M:%S')}"
            )
            return "retry"

        # 시간이 이미 지났다면 재시도 worker가 처리할 수 있게 둔다.
        # 전체 발송에서는 중복 시도를 피하기 위해 건너뜀.
        print(
            f"[재시도 예정 건너뜀] "
            f"{title} ({chat_id})"
        )
        return "retry"

    try:
        await forward_registered_post(chat_id)

        clear_room_retry(chat_id)

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

    except SlowModeWaitError as e:
        wait_seconds = set_room_retry(
            chat_id,
            title,
            e.seconds,
            "SlowMode",
            trigger_type
        )

        print(
            f"[SlowMode 격리] "
            f"{title} - {wait_seconds}초 후 재시도"
        )

        return "retry"

    except FloodWaitError as e:
        # 핵심: FloodWait가 난 방만 격리하고
        # 전체 발송은 다음 방으로 계속 진행
        wait_seconds = set_room_retry(
            chat_id,
            title,
            e.seconds,
            "FloodWait",
            trigger_type
        )

        print(
            f"[FloodWait 방 격리] "
            f"{title} - {wait_seconds}초 후 재시도"
        )

        return "retry"

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


async def send_to_all_rooms(trigger_type="자동"):
    rooms = get_rooms()

    if not rooms:
        print("등록된 방이 없습니다.")
        return 0, 0, 0

    success = 0
    failed = 0
    retry = 0
    processed_in_batch = 0

    async with SEND_BATCH_LOCK:
        total_rooms = len(rooms)

        for index, room in enumerate(rooms, 1):
            result = await send_one_room(
                room,
                trigger_type
            )

            if result == "success":
                success += 1
                processed_in_batch += 1

                # 방 하나 성공 후 다음 방까지 기본 간격
                await asyncio.sleep(
                    SEND_DELAY_SECONDS
                )

            elif result == "retry":
                retry += 1
                # 격리된 방은 바로 넘기되 너무 촘촘하지 않게 짧게 쉼
                await asyncio.sleep(1)

            else:
                failed += 1
                await asyncio.sleep(1)

            # 성공 발송 BATCH_SIZE개마다 긴 휴식
            if (
                BATCH_SIZE > 0
                and processed_in_batch >= BATCH_SIZE
                and index < total_rooms
            ):
                print(
                    f"[레이트리밋 휴식] "
                    f"{processed_in_batch}개 성공 발송 완료 - "
                    f"{BATCH_PAUSE_SECONDS}초 대기"
                )

                await asyncio.sleep(
                    BATCH_PAUSE_SECONDS
                )

                processed_in_batch = 0

    return success, failed, retry


# =========================
# 재시도 worker
# =========================
async def retry_one_room(retry_row):
    chat_id = retry_row["chat_id"]

    if chat_id in RETRY_IN_PROGRESS:
        return

    RETRY_IN_PROGRESS.add(chat_id)

    try:
        room = get_room(chat_id)

        # 방이 삭제된 경우 재시도도 정리
        if not room:
            clear_room_retry(chat_id)
            return

        title = room["title"]
        trigger_type = retry_row["trigger_type"]

        try:
            await forward_registered_post(chat_id)

            clear_room_retry(chat_id)

            add_send_log(
                chat_id,
                title,
                "성공",
                trigger_type=f"{trigger_type}-재시도"
            )

            print(
                f"[재시도 성공] "
                f"{title} ({chat_id})"
            )

        except SlowModeWaitError as e:
            wait_seconds = set_room_retry(
                chat_id,
                title,
                e.seconds,
                "SlowMode",
                trigger_type
            )

            print(
                f"[재시도 SlowMode] "
                f"{title} - {wait_seconds}초 후 다시"
            )

        except FloodWaitError as e:
            wait_seconds = set_room_retry(
                chat_id,
                title,
                e.seconds,
                "FloodWait",
                trigger_type
            )

            print(
                f"[재시도 FloodWait] "
                f"{title} - {wait_seconds}초 후 다시"
            )

        except Exception as e:
            error_text = (
                f"{type(e).__name__}: {e}"
            )

            clear_room_retry(chat_id)

            add_send_log(
                chat_id,
                title,
                "실패",
                error_text,
                f"{trigger_type}-재시도"
            )

            print(
                f"[재시도 실패] "
                f"{title} ({chat_id}): "
                f"{error_text}"
            )

    finally:
        RETRY_IN_PROGRESS.discard(chat_id)


async def retry_worker():
    while True:
        try:
            due_rows = get_due_retries(limit=10)

            for retry_row in due_rows:
                await retry_one_room(retry_row)
                await asyncio.sleep(
                    SEND_DELAY_SECONDS
                )

        except Exception as e:
            print(
                f"[재시도 worker 오류] "
                f"{type(e).__name__}: {e}"
            )

        await asyncio.sleep(
            RETRY_CHECK_SECONDS
        )


# =========================
# 정각 스케줄러
# =========================
async def scheduler():
    """
    정각 고정이 아니라 '이전 전체 발송이 끝난 시점'을 기준으로
    INTERVAL_HOURS 만큼 기다린 뒤 다음 발송을 시작한다.

    예:
    03:10 시작 → 03:40 완료 → 1시간 대기 → 04:40 다음 시작
    발송이 늦어지면 다음 시작 시간도 자연스럽게 뒤로 밀린다.
    """
    while True:
        started_at = datetime.now(TIMEZONE)

        print(
            "[자동발송 시작] "
            + started_at.strftime("%Y-%m-%d %H:%M:%S KST")
        )

        success, failed, retry = await send_to_all_rooms("자동")

        finished_at = datetime.now(TIMEZONE)
        next_run = finished_at + timedelta(hours=INTERVAL_HOURS)

        print(
            f"[자동발송 완료] "
            f"성공={success} 실패={failed} 대기방={retry}"
        )
        print(
            "다음 자동발송:",
            next_run.strftime("%Y-%m-%d %H:%M:%S KST")
        )

        await asyncio.sleep(
            INTERVAL_HOURS * 3600
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
        retry_rows = get_retry_list(30)

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
                "방별 발송 간격: "
                f"{SEND_DELAY_SECONDS}초"
            ),
            (
                "재시도 확인 주기: "
                f"{RETRY_CHECK_SECONDS}초"
            ),
            (
                "배치 크기: "
                f"{BATCH_SIZE}개"
            ),
            (
                "배치 휴식: "
                f"{BATCH_PAUSE_SECONDS}초"
            ),
            (
                "격리/재시도 대기 방: "
                f"{get_retry_count()}개"
            ),
            "발송 방식: 채널 게시글 전달(Forward)",
        ]

        for row in retry_rows:
            retry_at = row["retry_at"].astimezone(TIMEZONE)

            lines.append(
                f"\n⏳ {row['room_title'] or row['chat_id']}\n"
                f"{row['reason']} / "
                f"{retry_at.strftime('%m/%d %H:%M:%S')}"
            )

        await respond(
            event,
            "\n".join(lines)[:4000]
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
            f"대기방 {retry}"
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
        f"자동발송 휴식간격="
        f"{INTERVAL_HOURS}시간 (이전 발송 완료 후 기준)"
    )
    print(
        f"방별 발송 대기="
        f"{SEND_DELAY_SECONDS}초"
    )
    print(
        f"레이트리밋 배치="
        f"{BATCH_SIZE}개 / "
        f"휴식 {BATCH_PAUSE_SECONDS}초"
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
        "FloodWait/SlowMode "
        "방별 격리 모드 활성화"
    )

    asyncio.create_task(
        scheduler()
    )
    asyncio.create_task(
        retry_worker()
    )

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
