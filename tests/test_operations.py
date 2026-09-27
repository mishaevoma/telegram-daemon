import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import types

from telegram_daemon.cli import backup, init, instance_lock, restore
from telegram_daemon.config import Credentials, load_config
from telegram_daemon.content import media_info
from telegram_daemon.daemon import Daemon
from telegram_daemon.store import Store


def test_init_and_config_are_private_and_validate_settings(tmp_path):
    path = tmp_path / "settings" / "config.toml"
    init(path)
    config = load_config(path)
    assert config.monitor.mode == "personal"
    assert config.monitor.include_outgoing
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    path.write_text('timezone = "Europe/Lisbon"\n[monitor]\nmode = "typo"\n')
    with pytest.raises(ValueError, match="monitor.mode"):
        load_config(path)
    path.write_text('[monitor]\ninclude_outgoing = "false"\n')
    with pytest.raises(ValueError, match="boolean"):
        load_config(path)


def test_single_instance_lock_and_read_only_history(config, store, make_message):
    with instance_lock(config.data_dir), pytest.raises(ValueError, match="already running"):
        with instance_lock(config.data_dir):
            pass
    store.ingest(make_message("Example"), False, config)
    reader = Store(config.data_dir / "history.sqlite3", read_only=True)
    try:
        assert reader.search("Example")[0]["text"] == "Example"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.set("anything", 1)
    finally:
        reader.close()


def test_backup_restore_retains_media_and_pauses_old_notifications(
    tmp_path, config, store, make_message
):
    media_root = config.data_dir / "media"
    media_root.mkdir()
    (media_root / "asset.pdf").write_bytes(b"captured bytes")
    value = make_message("Example")
    value["media"] = {
        "kind": "document",
        "label": "File",
        "media_id": 4,
        "state": "metadata_only",
        "size": 14,
    }
    version_id = store.ingest(value, False, config, 1)
    store.media_result(version_id, "captured", "asset.pdf")
    store.deletions("common", [10], config, 1, 99)
    event = store.pending_event()
    store.prepare(event["id"], [{"kind": "text", "text": "notification"}])
    destination = tmp_path / "backup"
    backup(config, store, destination)
    restored_config = replace(config, data_dir=tmp_path / "restored")
    restored_config.data_dir.mkdir()
    restore(restored_config, destination)
    recovered = Store(restored_config.data_dir / "history.sqlite3")
    try:
        assert recovered.get("paused") is True
        assert recovered.get("bot_ready") is False
        assert recovered.next_part() is None
        assert recovered.stats()["deliveries"]["uncertain"] == 1
        assert recovered.search("Example")[0]["text"] == "Example"
        assert (restored_config.data_dir / "media" / "asset.pdf").read_bytes() == b"captured bytes"
    finally:
        recovered.close()
    (destination / "media" / "asset.pdf").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="verification"):
        restore(replace(config, data_dir=tmp_path / "another"), destination)


async def test_media_capture_after_source_deletion_uses_cached_object(config, store, make_message):
    document = types.Document(
        id=123,
        access_hash=45,
        file_reference=b"ref",
        date=datetime.now(UTC),
        mime_type="application/pdf",
        size=5,
        dc_id=2,
        attributes=[types.DocumentAttributeFilename("../../test.pdf")],
    )
    message = types.Message(
        id=10, peer_id=types.PeerUser(2), media=types.MessageMediaDocument(document=document)
    )
    value = make_message(media=message.media)
    version_id = store.ingest(value, False, config, 1)
    user, bot = Mock(), Mock()

    async def download(source, *, file, progress_callback):
        assert source is message
        progress_callback(5, 5)
        file.write(b"bytes")

    user.download_media = AsyncMock(side_effect=download)
    user.get_messages = AsyncMock(return_value=None)
    daemon = Daemon(config, Credentials(1, "hash", "123:token"), store, user=user, bot=bot)
    daemon.media_root.mkdir()
    daemon.media_cache[version_id] = message
    store.deletions("common", [10], config, 1, 99)
    await daemon.download(store.version(version_id))
    result = store.version(version_id)["media"]
    assert result["state"] == "captured"
    assert "/" not in result["path"]
    assert (daemon.media_root / result["path"]).read_bytes() == b"bytes"
    user.get_messages.assert_not_awaited()
    assert not list(daemon.media_root.glob(".capture-*"))


def test_video_sticker_classification_is_independent_of_attribute_order():
    attributes = [
        types.DocumentAttributeSticker("🌟", types.InputStickerSetEmpty()),
        types.DocumentAttributeVideo(2, 512, 512),
        types.DocumentAttributeAnimated(),
    ]
    for attrs in (attributes, list(reversed(attributes))):
        doc = types.Document(
            id=1,
            access_hash=2,
            file_reference=b"",
            date=datetime.now(UTC),
            mime_type="video/webm",
            size=5,
            dc_id=2,
            attributes=attrs,
        )
        message = types.Message(
            id=1, peer_id=types.PeerUser(2), media=types.MessageMediaDocument(document=doc)
        )
        assert media_info(message)["kind"] == "sticker"


async def test_daemon_starts_all_workers_and_shuts_down_cleanly(config, store):
    user = Mock()
    user.connect = AsyncMock()
    user.disconnect = AsyncMock()
    user.is_user_authorized = AsyncMock(return_value=True)
    user.get_me = AsyncMock(return_value=SimpleNamespace(id=1))
    user.catch_up = AsyncMock()
    user.disconnected = asyncio.get_running_loop().create_future()
    user.is_connected.return_value = True
    bot = Mock()
    bot.call = AsyncMock(return_value={"id": 99, "username": "example_bot"})
    bot.close = AsyncMock()
    daemon = Daemon(config, Credentials(1, "hash", "123:token"), store, user=user, bot=bot)

    async def stop_after_start():
        await asyncio.sleep(0.02)
        daemon.stop.set()
        await asyncio.sleep(10)

    daemon.bot_loop = stop_after_start
    await asyncio.wait_for(daemon.run(), 2)
    assert store.get("running") is False
    assert store.get("owner_id") == 1
    assert store.get("bot_id") == 99
    user.catch_up.assert_awaited_once()
    user.disconnect.assert_awaited_once()
    bot.close.assert_awaited_once()
