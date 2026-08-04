# Telegram Userbot - 방 링크 / 게시글 링크 등록형

## 방 등록
어느 채팅에서든:

`/방등록 https://t.me/groupname`

비공개 그룹/채널은 그 방의 아무 게시글 링크 사용:

`/방등록 https://t.me/c/123456789/10`

자동화 계정이 해당 방에 이미 들어가 있고 메시지 전송 권한이 있어야 합니다.

## 게시글 등록

`/글등록 https://t.me/channelname/123`

또는 비공개 게시글:

`/글등록 https://t.me/c/123456789/123`

## 명령어

- `/방등록 방링크`
- `/방삭제 방링크`
- `/방목록`
- `/글등록 게시글링크`
- `/글확인`
- `/글삭제`
- `/발송`
- `/도움말`

## Railway Variables

- API_ID
- API_HASH
- SESSION_STRING
- INTERVAL_HOURS=1
- SEND_DELAY_SECONDS=2.0
- DB_PATH=/data/userbot.db

## SQLite

Railway Volume을 `/data`에 마운트하고
`DB_PATH=/data/userbot.db` 로 설정하면 등록 정보가 유지됩니다.

## 주의

방 수가 많으면 Telegram FloodWait이 발생할 수 있습니다.
이 코드는 FloodWait이 오면 지정된 시간만큼 자동 대기 후 계속 진행합니다.
