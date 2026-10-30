"""telemsg — 텔레그램 단체방에서 특정 인물의 메시지를 보관하고 아이폰에서 볼 수 있게 복사한다.

내 텔레그램 계정으로 로그인해 단체방을 지켜보다가, 지정한 사람이 메시지를 보내면
1) 로컬 SQLite DB와 data/media 폴더에 원문과 첨부파일을 저장하고
2) "저장한 메시지"(또는 내가 만든 비공개 채널)로 복사본을 보낸다.

복사본은 원본과 별개의 메시지라서, 원본이 삭제되어도 아이폰에서 계속 볼 수 있다.
원본이 수정·삭제되면 복사본에 답장 형태로 알림을 남긴다.

사용법:
    python telemsg.py login                 # 최초 1회 로그인 (전화번호, 인증코드 입력)
    python telemsg.py chats                 # 내 채팅 목록과 ID 보기
    python telemsg.py members <채팅> [검색어] # 단체방 멤버의 ID 보기
    python telemsg.py run                   # 보관 시작 (계속 실행)
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient, events, types, utils

log = logging.getLogger("telemsg")

TEXT_LIMIT = 4096     # 텔레그램 일반 메시지 최대 길이
CAPTION_LIMIT = 1024  # 사진·파일 캡션 최대 길이


# ─── 설정 ────────────────────────────────────────────────────────────────


@dataclass
class Config:
    api_id: int
    api_hash: str
    chats: list
    users: list
    dest: object
    data_dir: Path
    tz: ZoneInfo
    sync_minutes: int
    backfill: int
    max_media_mb: int


def parse_ref(value):
    """'-100123' 같은 숫자는 int로, '@name'이나 채팅방 이름은 문자열 그대로 둔다."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


def split_list(value):
    return [parse_ref(v) for v in value.split(",") if v.strip()]


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"환경변수 {name} 가 비어 있습니다. .env 파일을 확인하세요 (.env.example 참고).")
    return value


def load_config(need_targets):
    load_dotenv()
    return Config(
        api_id=int(require_env("API_ID")),
        api_hash=require_env("API_HASH"),
        chats=split_list(require_env("CHATS")) if need_targets else [],
        users=split_list(require_env("USERS")) if need_targets else [],
        dest=parse_ref(os.environ.get("DEST", "me") or "me"),
        data_dir=Path(os.environ.get("DATA_DIR", "data")),
        tz=ZoneInfo(os.environ.get("TIMEZONE", "Asia/Seoul")),
        sync_minutes=int(os.environ.get("SYNC_MINUTES", "10")),
        backfill=int(os.environ.get("BACKFILL", "300")),
        max_media_mb=int(os.environ.get("MAX_MEDIA_MB", "50")),
    )


# ─── 로컬 보관소 (SQLite) ────────────────────────────────────────────────


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                chat_id     INTEGER NOT NULL,
                msg_id      INTEGER NOT NULL,
                sender_id   INTEGER,
                sender_name TEXT,
                date        TEXT NOT NULL,
                text        TEXT,
                media_path  TEXT,
                copy_id     INTEGER,   -- 복사본 메시지 ID (NULL이면 아직 못 보냄)
                edited_at   TEXT,
                deleted_at  TEXT,
                PRIMARY KEY (chat_id, msg_id)
            );
            CREATE TABLE IF NOT EXISTS scan_state (
                chat_id      INTEGER PRIMARY KEY,
                last_scanned INTEGER NOT NULL
            );
            """
        )

    def add(self, chat_id, msg_id, sender_id, sender_name, date, text):
        """새 메시지면 저장하고 True, 이미 있으면 False."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages (chat_id, msg_id, sender_id, sender_name, date, text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, msg_id, sender_id, sender_name, date.isoformat(), text),
        )
        self.db.commit()
        return cur.rowcount == 1

    def get(self, chat_id, msg_id):
        return self.db.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
        ).fetchone()

    def set_media(self, chat_id, msg_id, path):
        self.db.execute(
            "UPDATE messages SET media_path = ? WHERE chat_id = ? AND msg_id = ?",
            (path, chat_id, msg_id),
        )
        self.db.commit()

    def set_copy(self, chat_id, msg_id, copy_id):
        self.db.execute(
            "UPDATE messages SET copy_id = ? WHERE chat_id = ? AND msg_id = ?",
            (copy_id, chat_id, msg_id),
        )
        self.db.commit()

    def update_text(self, chat_id, msg_id, text, edited_at):
        self.db.execute(
            "UPDATE messages SET text = ?, edited_at = ? WHERE chat_id = ? AND msg_id = ?",
            (text, edited_at.isoformat(), chat_id, msg_id),
        )
        self.db.commit()

    def mark_deleted(self, chat_ids, msg_ids, deleted_at):
        """아직 삭제 표시가 없는 메시지를 삭제됨으로 표시하고, 새로 표시된 행을 돌려준다."""
        if not chat_ids or not msg_ids:
            return []
        where = (
            f"chat_id IN ({','.join('?' * len(chat_ids))})"
            f" AND msg_id IN ({','.join('?' * len(msg_ids))}) AND deleted_at IS NULL"
        )
        params = [*chat_ids, *msg_ids]
        rows = self.db.execute(f"SELECT * FROM messages WHERE {where}", params).fetchall()
        self.db.execute(f"UPDATE messages SET deleted_at = ? WHERE {where}", [deleted_at.isoformat(), *params])
        self.db.commit()
        return rows

    def pending(self):
        """저장은 됐지만 복사본 전송에 실패한 메시지."""
        return self.db.execute(
            "SELECT * FROM messages WHERE copy_id IS NULL ORDER BY chat_id, msg_id"
        ).fetchall()

    def recent_alive(self, chat_id, since, limit=200):
        return [
            r["msg_id"]
            for r in self.db.execute(
                "SELECT msg_id FROM messages WHERE chat_id = ? AND deleted_at IS NULL AND date >= ?"
                " ORDER BY msg_id DESC LIMIT ?",
                (chat_id, since.isoformat(), limit),
            )
        ]

    def last_scanned(self, chat_id):
        row = self.db.execute(
            "SELECT last_scanned FROM scan_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["last_scanned"] if row else 0

    def set_scanned(self, chat_id, msg_id):
        self.db.execute(
            "INSERT INTO scan_state (chat_id, last_scanned) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET last_scanned = MAX(last_scanned, excluded.last_scanned)",
            (chat_id, msg_id),
        )
        self.db.commit()


# ─── 메시지 꾸미기 ───────────────────────────────────────────────────────


def chunks(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def fmt_time(dt, tz):
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def make_header(sender_name, chat_title, date, tz):
    return f"👤 {sender_name} · 💬 {chat_title}\n🕒 {fmt_time(date, tz)}"


def describe_media(msg):
    """파일로 받을 수 없는 첨부(투표, 위치 등)를 글로 설명한다."""
    media = msg.media
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return ""
    if isinstance(media, types.MessageMediaPoll):
        question = media.poll.question
        return f"[투표] {getattr(question, 'text', question)}"
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        geo = media.geo
        return f"[위치] https://maps.google.com/?q={geo.lat},{geo.long}"
    if isinstance(media, types.MessageMediaContact):
        return f"[연락처] {media.first_name} {media.last_name} {media.phone_number}".strip()
    return f"[첨부: {type(media).__name__.removeprefix('MessageMedia')}]"


# ─── 보관기 ──────────────────────────────────────────────────────────────


class Archiver:
    def __init__(self, client, cfg, store):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.media_dir = cfg.data_dir / "media"
        self.chats = {}        # chat_id -> 채팅 엔티티
        self.titles = {}       # chat_id -> 채팅방 이름
        self.user_ids = set()  # 보관할 사람들의 ID
        self.in_flight = set() # 지금 복사본을 보내는 중인 (chat_id, msg_id)
        self.dest = None

    async def setup(self):
        dialogs = await self.client.get_dialogs()  # 엔티티 캐시 채우기 (비공개 채널 ID 해석에 필요)
        by_name = {d.name: d.entity for d in dialogs}

        for ref in self.cfg.chats:
            entity = by_name.get(ref) if isinstance(ref, str) else None
            entity = entity or await self.client.get_entity(ref)
            chat_id = utils.get_peer_id(entity)
            self.chats[chat_id] = entity
            self.titles[chat_id] = utils.get_display_name(entity)

        for ref in self.cfg.users:
            self.user_ids.add(ref if isinstance(ref, int) else (await self.client.get_entity(ref)).id)

        self.dest = await self.client.get_input_entity(self.cfg.dest)
        log.info("감시할 채팅: %s", ", ".join(f"{t} ({i})" for i, t in self.titles.items()))
        log.info("보관할 사람 ID: %s", ", ".join(map(str, sorted(self.user_ids))))

    # 일반 그룹(슈퍼그룹이 아닌 것)은 삭제 알림에 채팅 ID가 안 실려 온다.
    @property
    def basic_chat_ids(self):
        return [cid for cid, ent in self.chats.items() if not isinstance(ent, types.Channel)]

    def register(self):
        chats, users = list(self.chats), list(self.user_ids)
        self.client.add_event_handler(self.on_new, events.NewMessage(chats=chats, from_users=users))
        self.client.add_event_handler(self.on_edit, events.MessageEdited(chats=chats, from_users=users))
        self.client.add_event_handler(self.on_deleted, events.MessageDeleted())

    # ── 이벤트 처리 ──

    async def on_new(self, event):
        await self.archive(event.message)

    async def on_edit(self, event):
        msg = event.message
        row = self.store.get(msg.chat_id, msg.id)
        if row is None:
            await self.archive(msg)
            return
        if (msg.message or "") == (row["text"] or ""):
            return
        edited_at = msg.edit_date or datetime.now(timezone.utc)
        self.store.update_text(msg.chat_id, msg.id, msg.message, edited_at)
        log.info("수정됨: %s #%s", self.titles.get(msg.chat_id), msg.id)
        if row["copy_id"]:
            await self.notify(row["copy_id"], f"✏️ 원본이 수정됨 ({fmt_time(edited_at, self.cfg.tz)})\n\n{msg.message or ''}")

    async def on_deleted(self, event):
        chat_ids = [event.chat_id] if event.chat_id else self.basic_chat_ids
        chat_ids = [c for c in chat_ids if c in self.chats]
        await self.handle_deleted(chat_ids, event.deleted_ids)

    async def handle_deleted(self, chat_ids, msg_ids):
        now = datetime.now(timezone.utc)
        for row in self.store.mark_deleted(chat_ids, msg_ids, now):
            log.info("삭제됨: %s #%s", self.titles.get(row["chat_id"]), row["msg_id"])
            if row["copy_id"]:
                await self.notify(row["copy_id"], f"🗑 원본이 삭제됨 ({fmt_time(now, self.cfg.tz)}) — 이 복사본은 남아 있습니다.")

    # ── 보관·복사 ──

    async def archive(self, msg):
        if msg.chat_id not in self.chats or msg.sender_id not in self.user_ids:
            return
        sender = await msg.get_sender()
        name = utils.get_display_name(sender) if sender else str(msg.sender_id)
        if not self.store.add(msg.chat_id, msg.id, msg.sender_id, name, msg.date, msg.message):
            return  # 이미 보관한 메시지
        log.info("보관: %s · %s #%s", self.titles[msg.chat_id], name, msg.id)
        await self.deliver(self.store.get(msg.chat_id, msg.id), msg)

    async def deliver(self, row, msg):
        """첨부파일을 내려받고 복사본을 보낸다. 실패하면 다음 동기화 때 다시 시도한다."""
        chat_id, msg_id = row["chat_id"], row["msg_id"]
        if (chat_id, msg_id) in self.in_flight:
            return  # 실시간 처리와 동기화가 같은 메시지를 동시에 보내지 않도록
        self.in_flight.add((chat_id, msg_id))
        try:
            media_path = row["media_path"]
            if not (media_path and Path(media_path).exists()) and msg is not None:
                media_path = await self.download(msg)
                self.store.set_media(chat_id, msg_id, media_path)
            copy_id = await self.send_copy(row, msg, media_path)
            self.store.set_copy(chat_id, msg_id, copy_id)
        except Exception:
            log.exception("복사본 전송 실패 (%s #%s) — 나중에 다시 시도합니다", chat_id, msg_id)
        finally:
            self.in_flight.discard((chat_id, msg_id))

    async def download(self, msg):
        if msg.media is None or isinstance(msg.media, types.MessageMediaWebPage) or msg.file is None:
            return None
        size = msg.file.size or 0
        if size > self.cfg.max_media_mb * 1024 * 1024:
            log.warning("첨부파일이 너무 큼 (%.1f MB) — 건너뜀", size / 1024 / 1024)
            return None
        folder = self.media_dir / str(msg.chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = await msg.download_media(file=str(folder / f"{msg.id}{msg.file.ext or ''}"))
        return str(path) if path else None

    async def send_copy(self, row, msg, media_path):
        date = datetime.fromisoformat(row["date"])
        header = make_header(row["sender_name"], self.titles[row["chat_id"]], date, self.cfg.tz)
        text = row["text"] or ""
        extra = describe_media(msg) if msg is not None and not media_path else ""
        if msg is None:
            extra = "(원본은 이미 삭제되어 보관된 내용으로 보냅니다)"
        body = "\n\n".join(part for part in (header, text, extra) if part)

        if media_path:
            caption, rest = (body, "") if len(body) <= CAPTION_LIMIT else (header, text)
            sent = await self.client.send_file(
                self.dest, media_path, caption=caption, parse_mode=None,
                voice_note=bool(msg and msg.voice), video_note=bool(msg and msg.video_note),
            )
            first_id = sent.id
            for part in chunks(rest, TEXT_LIMIT) if rest else []:
                await self.client.send_message(self.dest, part, reply_to=first_id, parse_mode=None)
            return first_id

        parts = chunks(body, TEXT_LIMIT)
        first = await self.client.send_message(self.dest, parts[0], parse_mode=None, link_preview=False)
        for part in parts[1:]:
            await self.client.send_message(self.dest, part, reply_to=first.id, parse_mode=None, link_preview=False)
        return first.id

    async def notify(self, copy_id, text):
        try:
            await self.client.send_message(self.dest, text, reply_to=copy_id, parse_mode=None)
        except Exception:
            log.exception("알림 전송 실패")

    # ── 주기적 동기화 ──

    async def sync(self):
        """프로그램이 꺼져 있던 동안 놓친 메시지·삭제를 따라잡는다."""
        for chat_id, entity in self.chats.items():
            last = self.store.last_scanned(chat_id)
            if last:
                messages = [m async for m in self.client.iter_messages(entity, min_id=last, reverse=True)]
            else:  # 처음 실행: 최근 BACKFILL개 메시지부터 훑는다
                messages = [m async for m in self.client.iter_messages(entity, limit=self.cfg.backfill)]
                messages.reverse()
            for msg in messages:
                if isinstance(msg, types.Message):
                    await self.archive(msg)
            if messages:
                self.store.set_scanned(chat_id, max(m.id for m in messages))

            # 최근 7일간 보관한 메시지 중 원본이 사라진 것 찾기
            alive = self.store.recent_alive(chat_id, datetime.now(timezone.utc) - timedelta(days=7))
            if alive:
                found = await self.client.get_messages(entity, ids=alive)
                gone = [mid for mid, m in zip(alive, found) if m is None or isinstance(m, types.MessageEmpty)]
                await self.handle_deleted([chat_id], gone)

        for row in self.store.pending():
            entity = self.chats.get(row["chat_id"])
            if entity is None:
                continue
            msg = await self.client.get_messages(entity, ids=row["msg_id"])
            await self.deliver(row, msg if isinstance(msg, types.Message) else None)

    async def sync_loop(self):
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("동기화 중 오류")
            await asyncio.sleep(self.cfg.sync_minutes * 60)


# ─── 명령 ────────────────────────────────────────────────────────────────


def make_client(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(cfg.data_dir / "telemsg"), cfg.api_id, cfg.api_hash)
    client.flood_sleep_threshold = 300  # 전송 제한에 걸리면 5분까지는 기다렸다 재시도
    return client


async def cmd_login(cfg):
    async with make_client(cfg) as client:
        await client.start()
        me = await client.get_me()
        print(f"로그인 완료: {utils.get_display_name(me)} (ID {me.id})")
        print(f"세션 파일: {cfg.data_dir / 'telemsg.session'} — 비밀번호처럼 안전하게 보관하세요.")


async def cmd_chats(cfg):
    async with connect(cfg) as client:
        async for d in client.iter_dialogs():
            kind = "채널" if d.is_channel and not d.is_group else "그룹" if d.is_group else "개인"
            print(f"{d.id}\t{kind}\t{d.name}")


async def cmd_members(cfg, chat, search):
    async with connect(cfg) as client:
        ref = parse_ref(chat)
        entity = next((d.entity for d in await client.get_dialogs() if d.name == ref), None)
        entity = entity or await client.get_entity(ref)
        async for user in client.iter_participants(entity, search=search or ""):
            username = f"@{user.username}" if user.username else "-"
            print(f"{user.id}\t{username}\t{utils.get_display_name(user)}")


async def cmd_run(cfg):
    async with connect(cfg) as client:
        archiver = Archiver(client, cfg, Store(cfg.data_dir / "telemsg.db"))
        await archiver.setup()
        archiver.register()
        sync_task = asyncio.create_task(archiver.sync_loop())
        log.info("보관을 시작합니다. 새 메시지는 '%s'(으)로 복사됩니다.", cfg.dest)
        try:
            await client.run_until_disconnected()
        finally:
            sync_task.cancel()


@contextlib.asynccontextmanager
async def connect(cfg):
    """로그인된 세션으로만 연결한다 (login 외의 명령에서 인증코드를 묻지 않도록)."""
    client = make_client(cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            sys.exit("로그인이 필요합니다. 먼저 `python telemsg.py login` 을 실행하세요.")
        yield client
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="텔레그램 단체방에서 특정 인물의 메시지를 보관합니다.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="텔레그램 계정 로그인 (최초 1회)")
    sub.add_parser("chats", help="내 채팅 목록과 ID 출력")
    members = sub.add_parser("members", help="단체방 멤버의 ID 출력")
    members.add_argument("chat", help="채팅방 이름 또는 ID")
    members.add_argument("search", nargs="?", help="이름 검색어 (선택)")
    sub.add_parser("run", help="보관 시작")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    cfg = load_config(need_targets=args.cmd == "run")
    if args.cmd == "login":
        coro = cmd_login(cfg)
    elif args.cmd == "chats":
        coro = cmd_chats(cfg)
    elif args.cmd == "members":
        coro = cmd_members(cfg, args.chat, args.search)
    else:
        coro = cmd_run(cfg)
    asyncio.run(coro)


if __name__ == "__main__":
    main()
