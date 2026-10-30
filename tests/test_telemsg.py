import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telethon import types

import telemsg

SEOUL = ZoneInfo("Asia/Seoul")
GROUP_ID = -100111
BASIC_GROUP_ID = -222
TARGET = 42


def make_cfg(tmp_path):
    return telemsg.Config(
        api_id=1, api_hash="x", chats=[], users=[], dest="me", data_dir=Path(tmp_path),
        tz=SEOUL, sync_minutes=10, backfill=10, max_media_mb=50,
    )


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, dest, text, reply_to=None, **kwargs):
        self.sent.append((text, reply_to))
        return SimpleNamespace(id=1000 + len(self.sent))


def make_msg(msg_id, text, chat_id=GROUP_ID, sender_id=TARGET, media=None):
    async def get_sender():
        return types.User(id=sender_id, first_name="홍길동")

    return SimpleNamespace(
        id=msg_id, chat_id=chat_id, sender_id=sender_id, message=text, media=media, file=None,
        date=datetime(2026, 10, 30, 1, 5, tzinfo=timezone.utc), get_sender=get_sender,
        voice=None, video_note=None,
    )


def make_archiver(tmp_path):
    client = FakeClient()
    archiver = telemsg.Archiver(client, make_cfg(tmp_path), telemsg.Store(":memory:"))
    archiver.chats = {GROUP_ID: types.Channel(id=111, title="단체방", photo=None, date=None, megagroup=True),
                      BASIC_GROUP_ID: types.Chat(id=222, title="작은방", photo=None, participants_count=3,
                                                 date=None, version=1)}
    archiver.titles = {GROUP_ID: "단체방", BASIC_GROUP_ID: "작은방"}
    archiver.user_ids = {TARGET}
    archiver.dest = "me"
    return archiver, client


def test_store_dedup_and_scan_state():
    store = telemsg.Store(":memory:")
    now = datetime.now(timezone.utc)
    assert store.add(GROUP_ID, 1, TARGET, "a", now, "hi")
    assert not store.add(GROUP_ID, 1, TARGET, "a", now, "hi")
    store.set_scanned(GROUP_ID, 10)
    store.set_scanned(GROUP_ID, 5)
    assert store.last_scanned(GROUP_ID) == 10
    assert [r["msg_id"] for r in store.mark_deleted([GROUP_ID], [1, 2], now)] == [1]
    assert store.mark_deleted([GROUP_ID], [1], now) == []


def test_header_uses_timezone():
    date = datetime(2026, 10, 30, 1, 5, tzinfo=timezone.utc)
    assert telemsg.make_header("홍길동", "단체방", date, SEOUL) == "👤 홍길동 · 💬 단체방\n🕒 2026-10-30 10:05"


def test_chunks_splits_long_text():
    assert telemsg.chunks("abcde", 2) == ["ab", "cd", "e"]
    assert telemsg.chunks("", 2) == [""]


def test_archive_copies_target_messages_once(tmp_path):
    archiver, client = make_archiver(tmp_path)

    async def scenario():
        await archiver.archive(make_msg(1, "안녕하세요"))
        await archiver.archive(make_msg(1, "안녕하세요"))           # 중복
        await archiver.archive(make_msg(2, "다른 사람", sender_id=7))  # 대상 아님
    asyncio.run(scenario())

    assert len(client.sent) == 1
    text, reply_to = client.sent[0]
    assert text == "👤 홍길동 · 💬 단체방\n🕒 2026-10-30 10:05\n\n안녕하세요"
    assert archiver.store.get(GROUP_ID, 1)["copy_id"] == 1001


def test_long_message_is_split(tmp_path):
    archiver, client = make_archiver(tmp_path)
    asyncio.run(archiver.archive(make_msg(1, "가" * 5000)))
    assert len(client.sent) == 2
    assert client.sent[1][1] == 1001  # 이어지는 부분은 첫 메시지에 답장으로


def test_deleted_in_basic_group_is_reported(tmp_path):
    archiver, client = make_archiver(tmp_path)

    async def scenario():
        await archiver.archive(make_msg(5, "곧 지울 말", chat_id=BASIC_GROUP_ID))
        # 일반 그룹의 삭제 알림에는 chat_id가 없다
        await archiver.on_deleted(SimpleNamespace(chat_id=None, deleted_ids=[5]))
        await archiver.on_deleted(SimpleNamespace(chat_id=None, deleted_ids=[5]))
    asyncio.run(scenario())

    assert len(client.sent) == 2
    assert client.sent[1][0].startswith("🗑 원본이 삭제됨")
    assert client.sent[1][1] == 1001
    assert archiver.store.get(BASIC_GROUP_ID, 5)["deleted_at"] is not None


def test_edit_is_reported(tmp_path):
    archiver, client = make_archiver(tmp_path)

    async def scenario():
        await archiver.archive(make_msg(3, "처음"))
        edited = make_msg(3, "고친 내용")
        edited.edit_date = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)
        await archiver.on_edit(SimpleNamespace(message=edited))
    asyncio.run(scenario())

    assert client.sent[1] == ("✏️ 원본이 수정됨 (2026-10-30 11:00)\n\n고친 내용", 1001)
    assert archiver.store.get(GROUP_ID, 3)["text"] == "고친 내용"


def test_describe_poll_and_geo():
    poll = types.MessageMediaPoll(
        poll=types.Poll(id=1, hash=0, question=types.TextWithEntities(text="점심 뭐 먹지?", entities=[]), answers=[]),
        results=types.PollResults(),
    )
    assert telemsg.describe_media(SimpleNamespace(media=poll)) == "[투표] 점심 뭐 먹지?"
    geo = types.MessageMediaGeo(geo=types.GeoPoint(long=127.0, lat=37.5, access_hash=0))
    assert telemsg.describe_media(SimpleNamespace(media=geo)) == "[위치] https://maps.google.com/?q=37.5,127.0"


def test_pending_copy_is_not_sent_twice_concurrently(tmp_path):
    archiver, client = make_archiver(tmp_path)
    release = asyncio.Event()
    original_send = client.send_message

    async def slow_send(*args, **kwargs):
        await release.wait()
        return await original_send(*args, **kwargs)

    client.send_message = slow_send

    async def scenario():
        live = asyncio.create_task(archiver.archive(make_msg(9, "동시에")))
        await asyncio.sleep(0)
        await archiver.deliver(archiver.store.get(GROUP_ID, 9), None)  # 동기화의 재전송 시도
        release.set()
        await live
    asyncio.run(scenario())

    assert len(client.sent) == 1
