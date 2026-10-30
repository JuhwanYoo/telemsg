# telemsg

텔레그램 **단체방에서 특정 인물이 보낸 메시지**를 실시간으로 보관하고, 아이폰에서 볼 수 있도록
**저장한 메시지**나 **나만 있는 비공개 채널**로 복사해 주는 스크립트입니다.

- 복사본은 원본과 별개의 메시지입니다. 원본이 삭제되어도 아이폰에 그대로 남습니다.
- 사진, 동영상, 파일, 음성 메시지도 내려받아 다시 올립니다. 투표와 위치는 글로 남깁니다.
- 원본이 **수정되거나 삭제되면** 복사본에 답장으로 알림이 달립니다. 예: `🗑 원본이 삭제됨 (2026-10-30 10:05)`
- 원문과 첨부파일은 `data/telemsg.db`(SQLite)와 `data/media/` 폴더에도 저장됩니다.
- `SYNC_MINUTES`(기본 10분)마다 놓친 메시지와 삭제 여부를 다시 확인합니다. 그래서 스크립트가 잠시 꺼져 있었어도,
  그 사이 **삭제되지 않은** 메시지는 따라잡습니다.

> ⚠️ 스크립트가 꺼져 있는 동안 올라왔다가 삭제된 메시지는 되살릴 수 없습니다. 항상 켜져 있는 곳에서 실행하세요.

## 1. 준비

1. https://my.telegram.org 에 로그인한 뒤 **API development tools**에서 앱을 만들고 `api_id`와 `api_hash`를 받습니다.
2. (권장) 아이폰 텔레그램에서 **새 채널**을 만들고 **비공개**로 설정합니다. 이름은 예를 들어 `○○님 메시지 보관`.
   이 채널에 복사본이 모이고, 알림도 따로 켜고 끌 수 있습니다. 채널을 만들지 않으면 "저장한 메시지"로 보냅니다.
3. 설정 파일을 만듭니다.

```bash
cp .env.example .env
```

`.env` 파일에서 `API_ID`, `API_HASH`, `CHATS`, `USERS`, `DEST`를 채워 넣습니다. 채팅방과 사람의 ID는 아래 2단계에서 확인합니다.

## 2-A. 내 컴퓨터(맥/리눅스)에서 실행

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python telemsg.py login
```

전화번호, 텔레그램으로 온 인증코드, (설정했다면) 2단계 인증 비밀번호를 입력합니다.

```bash
.venv/bin/python telemsg.py chats
```

단체방 ID(`-100…`)와 보관용 비공개 채널 ID를 확인해 `.env`의 `CHATS`와 `DEST`에 넣습니다.

```bash
.venv/bin/python telemsg.py members "우리 단체방" 홍길동
```

보관할 사람의 숫자 ID를 확인해 `.env`의 `USERS`에 넣습니다.

```bash
.venv/bin/python telemsg.py run
```

## 2-B. 서버(VPS 등)에서 Docker로 실행

```bash
docker compose run --rm telemsg login
```

```bash
docker compose run --rm telemsg chats
```

```bash
docker compose up -d
```

로그는 `docker compose logs -f`로 볼 수 있습니다. 서버가 재부팅되어도 자동으로 다시 시작됩니다.

## 아이폰에서 보기

보관용 채널(또는 저장한 메시지)을 열면 아래처럼 쌓여 있습니다.

```
👤 홍길동 · 💬 우리 단체방
🕒 2026-10-30 10:05

내일 회의는 3시로 바꿀게요
   └ 🗑 원본이 삭제됨 (2026-10-30 10:12) — 이 복사본은 남아 있습니다.
```

채널을 상단에 고정해 두면 확인하기 편합니다.

## 설정 항목 (`.env`)

| 이름 | 설명 | 기본값 |
|---|---|---|
| `API_ID`, `API_HASH` | my.telegram.org에서 발급받은 값 | (필수) |
| `CHATS` | 감시할 단체방: 이름, ID, @username. 여러 개면 쉼표로 구분 | (필수) |
| `USERS` | 보관할 사람: 숫자 ID 또는 @username. 여러 명이면 쉼표로 구분 | (필수) |
| `DEST` | 복사본을 보낼 곳: `me`(저장한 메시지) 또는 채널 ID | `me` |
| `TIMEZONE` | 표시할 시간대 | `Asia/Seoul` |
| `SYNC_MINUTES` | 놓친 메시지와 삭제 여부를 확인하는 주기(분) | `10` |
| `BACKFILL` | 처음 실행할 때 훑어볼 최근 메시지 수 | `300` |
| `MAX_MEDIA_MB` | 이보다 큰 첨부파일은 내려받지 않음 | `50` |

## 주의사항

- `data/telemsg.session` 파일은 **내 텔레그램 계정 로그인 그 자체**입니다. 절대 공유하거나 깃에 올리지 마세요.
  `.gitignore`에 이미 포함되어 있습니다. 유출이 의심되면 텔레그램 설정 → 기기에서 해당 세션을 종료하세요.
- 이 스크립트는 메시지를 **읽고, 내 보관 채널에 쓰기만** 합니다. 단체방에는 아무것도 보내지 않습니다.
- 한 번만 볼 수 있는 사진·영상과 비밀 채팅은 보관할 수 없습니다.
- 보관한 메시지는 개인 확인용으로만 쓰세요. 다른 사람에게 공유하면 문제가 될 수 있습니다.

## 테스트

```bash
.venv/bin/pip install pytest && PYTHONPATH=. .venv/bin/pytest -q tests
```
