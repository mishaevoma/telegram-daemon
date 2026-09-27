import sqlite3
import time

import pytest


def test_message_id_namespaces_do_not_cross_delete(store, config, make_message):
    config = config.with_mode("all")
    for channel in (None, 12, 13):
        store.ingest(make_message(channel=channel), False, config, 1)
    assert store.deletions("common", [10], config, 1, 99) == 1
    assert store.deletions("common", [10], config, 1, 99) == 0
    assert store.deletions("channel:12", [10], config, 1, 99) == 1
    assert store.deletions("channel:99", [10], config, 1, 99) == 0
    assert store.stats()["pending_events"] == 2
    assert store.pending_event()["before"]["chat_kind"] == "private"


def test_replay_reversion_and_out_of_order_edits(store, config, make_message):
    start = time.time()
    original = store.ingest(make_message("A"), False, config, 100)
    store.ingest(make_message("B", edited=start), True, config, 101)
    store.ingest(make_message("B", edited=start), True, config, 101)
    store.ingest(make_message("A", edited=start + 1), True, config, 102)
    store.ingest(make_message("old", edited=start - 1), True, config, 99)
    assert [row["text"] for row in store.history(original)] == ["A", "B", "A"]
    assert store.stats()["pending_events"] == 2
    store.deletions("common", [10], config, 1, 99)
    store.ingest(make_message("resurrection", edited=start + 10), True, config, 110)
    assert store.stats()["versions"] == 3


def test_capture_and_notification_intent_are_one_transaction(store, config, make_message):
    original = store.ingest(make_message(), False, config, 1)
    store.db.execute(
        "CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN "
        "SELECT RAISE(ABORT, 'simulated disk failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest(make_message("Changed", edited=time.time()), True, config, 2)
    assert len(store.history(original)) == 1
    assert store.stats()["pending_events"] == 0


def test_restart_keeps_intents_but_does_not_retry_ambiguous_sends(store, config, make_message):
    store.ingest(make_message(), False, config, 1)
    store.deletions("common", [10], config, 1, 99)
    event = store.pending_event()
    store.prepare(event["id"], [{"kind": "text", "text": "saved"}])
    part = store.next_part()
    store.part_state(part["id"], "sending")
    store.recover()
    assert store.next_part() is None
    assert store.stats()["deliveries"]["uncertain"] == 1
    assert store.retry() == 0
    assert store.retry(uncertain=True) == 1
    assert store.next_part()["payload"]["text"] == "saved"


def test_search_retains_old_versions_and_purge_clears_indexes_and_outbox(
    store, config, make_message
):
    original = store.ingest(make_message("Old deadline"), False, config, 1)
    store.ingest(make_message("New time", edited=time.time()), True, config, 2)
    store.deletions("common", [10], config, 1, 99)
    event = store.pending_event()
    store.prepare(event["id"], [{"kind": "text", "text": "private content"}])
    assert store.search("deadline")[0]["version_id"] == original
    assert store.purge(now=time.time() + 100 * 86400) == 2
    assert store.search("deadline") == []
    assert store.stats()["messages"] == 0
    assert store.stats()["pending_events"] == 0
    assert store.stats()["deliveries"] == {}


def test_first_seen_edit_is_a_useful_report_without_inventing_original(store, config, make_message):
    store.ingest(make_message("Updated", edited=time.time()), True, config, 1)
    event = store.pending_event()
    assert event["before"] is None
    assert event["after"]["text"] == "Updated"


def test_database_cannot_be_reused_for_another_account(store):
    with pytest.raises(ValueError, match="another Telegram account"):
        store.bind_account(3)


def test_caption_edit_reuses_an_already_captured_attachment(store, config, make_message):
    value = make_message("Before caption")
    value["media"] = {
        "kind": "photo",
        "label": "Photo",
        "media_id": 7,
        "state": "metadata_only",
        "size": 100,
    }
    original = store.ingest(value, False, config, 1)
    store.media_result(original, "captured", "photo.jpg")
    value.update(text="After caption", edit_at=time.time())
    updated = store.ingest(value, True, config, 2)
    assert store.version(updated)["media"]["state"] == "captured"
    assert store.version(updated)["media"]["path"] == "photo.jpg"
    assert store.media_job() is None
