# Telegram Userbot - 명령어형 자동발송

Railway Variables:
- API_ID
- API_HASH
- SESSION_STRING
- INTERVAL_HOURS=1
- DB_PATH=/data/userbot.db

명령어:
- /방등록
- /방삭제
- /방목록
- /글등록
- /글확인
- /발송
- /도움말

SESSION_STRING 생성:
```bash
pip install -r requirements.txt
python generate_session.py
```

생성 후 SESSION_ONLY.txt의 한 줄 전체를 Railway SESSION_STRING에 넣으세요.
복사 후 SESSION_ONLY.txt는 삭제하세요.

Railway Volume을 /data에 마운트하고 DB_PATH=/data/userbot.db 로 설정하면
등록된 방/글 정보가 재배포 후에도 유지됩니다.
