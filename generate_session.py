import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

print("Telegram SESSION_STRING 생성기")
print("API_HASH나 생성된 SESSION_STRING은 다른 사람에게 보내지 마세요.\n")

api_id = int(input("API_ID 숫자 입력: ").strip())
api_hash = input("API_HASH 입력: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n로그인을 진행합니다.")
    print("전화번호는 국가번호 포함 예: +821012345678")
    client.start()
    session_string = client.session.save()

print("\n===== SESSION_STRING =====")
print(session_string)
print("==========================")
print("\n위 긴 문자열 전체를 Railway의 SESSION_STRING 변수에 저장하세요.")
