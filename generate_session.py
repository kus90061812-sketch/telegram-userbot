from telethon.sync import TelegramClient
from telethon.sessions import StringSession

print("Telegram SESSION_STRING 생성기")
print("API_HASH와 SESSION_STRING은 절대 공유하지 마세요.\n")

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n전화번호는 국가번호 포함 예: +821012345678")
    client.start()
    session_string = client.session.save()

print("\n===== SESSION_STRING =====")
print(session_string)
print("==========================")
print("\n이 긴 문자열 전체를 Railway Variables의 SESSION_STRING에 넣으세요.")
