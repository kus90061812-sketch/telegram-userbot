import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# 전송 대상: @채널아이디 또는 -100... 형태의 채팅 ID
TARGET_CHAT = os.environ["TARGET_CHAT"]

# text = 직접 적은 문구 전송
# copy = 특정 채널/그룹의 기존 글을 그대로 다시 전송
SEND_MODE = os.environ.get("SEND_MODE", "text").lower()

AUTO_MESSAGE = os.environ.get("AUTO_MESSAGE", "")

# SEND_MODE=copy 일 때만 사용
SOURCE_CHAT = os.environ.get("SOURCE_CHAT", "")
SOURCE_MESSAGE_ID = int(os.environ.get("SOURCE_MESSAGE_ID", "0") or 0)

# 기본값: 매 1시간 정각
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", "1"))
TIMEZONE = ZoneInfo("Asia/Seoul")

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)

def seconds_until_next_slot():
    now = datetime.now(TIMEZONE)

    # 다음 정각 계산
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    # 2시간/3시간 등으로 바꿔도 00, 02, 04... 같은 식으로 맞춤
    if INTERVAL_HOURS > 1:
        while next_hour.hour % INTERVAL_HOURS != 0:
            next_hour += timedelta(hours=1)

    return max(1, (next_hour - now).total_seconds()), next_hour

async def send_post():
    if SEND_MODE == "copy":
        if not SOURCE_CHAT or not SOURCE_MESSAGE_ID:
            raise ValueError(
                "SEND_MODE=copy이면 SOURCE_CHAT, SOURCE_MESSAGE_ID가 필요합니다."
            )

        msg = await client.get_messages(SOURCE_CHAT, ids=SOURCE_MESSAGE_ID)
        if not msg:
            raise ValueError("원본 메시지를 찾지 못했습니다.")

        # 미디어가 있으면 미디어+본문을 새 글처럼 전송
        if msg.media:
            await client.send_file(
                TARGET_CHAT,
                msg.media,
                caption=msg.message or "",
            )
        else:
            await client.send_message(
                TARGET_CHAT,
                msg.message or "",
            )
    else:
        if not AUTO_MESSAGE:
            raise ValueError("SEND_MODE=text이면 AUTO_MESSAGE가 필요합니다.")
        await client.send_message(TARGET_CHAT, AUTO_MESSAGE)

async def main():
    await client.start()
    me = await client.get_me()
    print(f"로그인 완료: {me.first_name} / id={me.id}")
    print(f"전송 대상: {TARGET_CHAT}")
    print(f"전송 방식: {SEND_MODE}")
    print(f"간격: {INTERVAL_HOURS}시간 / 서울시간 정각 기준")

    while True:
        wait_seconds, next_run = seconds_until_next_slot()
        print(f"다음 전송: {next_run.strftime('%Y-%m-%d %H:%M:%S KST')}")
        await asyncio.sleep(wait_seconds)

        try:
            await send_post()
            print(f"[성공] {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"[전송 실패] {type(e).__name__}: {e}")

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
