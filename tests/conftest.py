from datetime import UTC, datetime

import pytest
from telethon import types

from telegram_daemon.config import Config
from telegram_daemon.content import snapshot
from telegram_daemon.store import Store


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path, min_free_mb=1)


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "history.sqlite3")
    database.bind_account(1)
    yield database
    database.close()


@pytest.fixture
def make_message():
    def make(
        text="Original",
        *,
        message_id=10,
        channel=None,
        edited=None,
        media=None,
        entities=None,
        outgoing=False,
    ):
        peer = types.PeerChannel(channel) if channel else types.PeerUser(2)
        message = types.Message(
            id=message_id,
            peer_id=peer,
            from_id=types.PeerUser(2),
            date=datetime(2026, 9, 26, 12, tzinfo=UTC),
            message=text,
            media=media,
            entities=entities,
            out=outgoing,
            edit_date=datetime.fromtimestamp(edited, UTC) if edited else None,
        )
        chat = (
            types.Channel(
                id=channel,
                title="A group",
                photo=types.ChatPhotoEmpty(),
                date=datetime.now(UTC),
                megagroup=True,
            )
            if channel
            else types.User(id=2, first_name="Alex", bot=False)
        )
        return snapshot(message, chat, types.User(id=2, first_name="Alex"), 1)

    return make
