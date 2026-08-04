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

with open("SESSION_ONLY.txt", "w", encoding="utf-8") as f:
    f.write(session_string)

print("\nSESSION_ONLY.txt 파일을 만들었습니다.")
print("파일 안의 한 줄 전체를 Railway SESSION_STRING에 넣으세요.")
print("복사 후 SESSION_ONLY.txt는 반드시 삭제하세요.")
