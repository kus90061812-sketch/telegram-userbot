# Telegram Userbot - 명령어형 자동발송

일반 Telegram 계정을 Telethon으로 로그인시켜,
Telegram 안에서 명령어로 방과 게시글을 등록하는 방식입니다.

## 명령어

- `/방등록` : 현재 방을 자동발송 대상으로 등록
- `/방삭제` : 현재 방을 자동발송 대상에서 삭제
- `/방목록` : 등록된 방 목록 확인
- `/글등록` : 답장한 게시글을 자동발송 글로 등록
- `/글확인` : 현재 등록된 글 정보 확인
- `/발송` : 즉시 테스트 발송
- `/도움말` : 명령어 확인

명령어는 로그인한 본인 계정의 outgoing 메시지만 처리합니다.
다른 사람이 같은 명령어를 보내도 설정은 변경되지 않습니다.

## Railway Variables

필수:
- `API_ID`
- `API_HASH`
- `SESSION_STRING`

선택:
- `INTERVAL_HOURS=1`
- `DB_PATH=/data/userbot.db`

## SQLite / Railway Volume

방 목록과 등록된 게시글은 SQLite에 저장합니다.

Railway에서 재배포나 재시작 후에도 설정을 유지하려면
Volume을 붙이고 마운트 경로를 `/data`로 설정한 뒤:

`DB_PATH=/data/userbot.db`

로 지정하는 것을 권장합니다.

Volume 없이 `DB_PATH=userbot.db`로 두면
배포 환경이 교체될 때 DB가 사라질 수 있습니다.

## 사용 순서

1. 자동화할 일반 계정이 대상 방/채널에 참여해 있어야 합니다.
2. 발송 받을 방에서 `/방등록`
3. 원본 게시글이 있는 채팅에서 그 게시글에 답장하여 `/글등록`
4. `/발송` 으로 테스트
5. 이후 서울시간 기준 매 정각 자동발송

예: 10:37에 실행 중이면 다음 발송은 11:00,
그 다음은 12:00, 13:00...

`INTERVAL_HOURS=2`이면 12:00, 14:00, 16:00... 형태입니다.

## 주의

- `API_HASH`, `SESSION_STRING`은 GitHub에 직접 넣지 마세요.
- Railway Variables에만 저장하세요.
- Telegram 정책을 위반하는 대량/스팸성 자동발송은 제한될 수 있습니다.
