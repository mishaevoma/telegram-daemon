from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_daemon.bot import AmbiguousDelivery, BotError, RetryableConnection
from telegram_daemon.config import Credentials, Monitor
from telegram_daemon.daemon import Daemon


def app(config, store):
    user = Mock()
    user.is_connected.return_value = True
    bot = Mock()
    bot.message = AsyncMock(return_value={"message_id": 1})
    bot.send = AsyncMock(return_value={"message_id": 2})
    daemon = Daemon(config, Credentials(123, "hash", "1:token"), store, user=user, bot=bot)
    daemon.owner, daemon.bot_id = 1, 99
    store.set("bot_ready", True)
    return daemon


def command(text, owner=1, chat_type="private"):
    return {
        "message": {"from": {"id": owner}, "chat": {"id": owner, "type": chat_type}, "text": text}
    }


def test_personal_mode_is_humans_only_and_can_exclude_outgoing(make_message):
    policy = Monitor(include_outgoing=False)
    human = make_message()
    assert policy.accepts(human, 1, 99)
    assert not policy.accepts({**human, "is_bot": True}, 1, 99)
    assert not policy.accepts({**human, "chat_id": 1}, 1, 99)
    assert not policy.accepts({**human, "chat_id": 99}, 1, 99)
    assert not policy.accepts({**human, "chat_id": 777000}, 1, 99)
    assert not policy.accepts(make_message(outgoing=True), 1, 99)
    assert not policy.accepts(make_message(channel=5), 1, 99)
    assert replace(policy, include_outgoing=True).accepts(make_message(outgoing=True), 1, 99)


async def test_bot_commands_are_owner_private_chat_only(config, store):
    daemon = app(config, store)
    await daemon.handle_command(command("/mode all", owner=2))
    await daemon.handle_command(command("/mode all", chat_type="group"))
    assert daemon.config.monitor.mode == "personal"
    daemon.bot.message.assert_not_awaited()
    await daemon.handle_command(command("/mode all"))
    assert store.get("mode") == "all"
    await daemon.handle_command(command("/mode personal"))
    assert daemon.config.monitor.mode == "personal"


async def test_switch_to_personal_suppresses_already_queued_group_reports(
    config, store, make_message
):
    daemon = app(config.with_mode("all"), store)
    store.ingest(make_message(channel=3), False, daemon.config, 1)
    store.deletions("channel:3", [10], daemon.config, 1, 99)
    await daemon.handle_command(command("/mode personal"))
    await daemon.deliver_one()
    daemon.bot.send.assert_not_awaited()
    assert store.pending_event() is None


@pytest.mark.parametrize(
    "error,state",
    [
        (AmbiguousDelivery("unknown"), "uncertain"),
        (RetryableConnection("offline"), "pending"),
        (BotError(429, "wait", 25), "pending"),
        (BotError(403, "blocked"), "blocked"),
    ],
)
async def test_delivery_failure_does_not_lose_capture(config, store, make_message, error, state):
    daemon = app(config, store)
    store.ingest(make_message(), False, config, 1)
    store.deletions("common", [10], config, 1, 99)
    daemon.bot.send.side_effect = error
    await daemon.deliver_one()
    assert store.stats()["messages"] == 1
    assert store.stats()["deliveries"][state] == 1
    if state in {"uncertain", "blocked"}:
        await daemon.deliver_one()
        assert daemon.bot.send.await_count == 1


async def test_rejected_formatting_retries_plain_text(config, store, make_message):
    daemon = app(config, store)
    store.ingest(make_message("<markup> 😀"), False, config, 1)
    store.deletions("common", [10], config, 1, 99)
    daemon.bot.send.side_effect = [BotError(400, "can't parse entities"), {"message_id": 7}]
    await daemon.deliver_one()
    await daemon.deliver_one()
    assert daemon.bot.send.call_args.args[1]["entities"] == []
    assert "<markup> 😀" in daemon.bot.send.call_args.args[1]["text"]
    assert store.stats()["deliveries"]["sent"] == 1


async def test_pause_stops_delivery_and_resume_preserves_queue(config, store, make_message):
    daemon = app(config, store)
    store.ingest(make_message(), False, config, 1)
    store.deletions("common", [10], config, 1, 99)
    await daemon.handle_command(command("/pause"))
    await daemon.deliver_one()
    daemon.bot.send.assert_not_awaited()
    await daemon.handle_command(command("/resume"))
    await daemon.deliver_one()
    daemon.bot.send.assert_awaited_once()


async def test_long_report_continuations_reply_to_the_first_message(config, store, make_message):
    daemon = app(config, store)
    store.ingest(make_message("😀" * 2000), False, config, 1)
    store.deletions("common", [10], config, 1, 99)
    await daemon.deliver_one()
    await daemon.deliver_one()
    assert daemon.bot.send.await_count == 2
    assert daemon.bot.send.call_args.args[1]["reply_to_id"] == 2
