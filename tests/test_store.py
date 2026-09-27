import sqlite3
import time

import pytest
from telethon import types


def live_location(latitude=3.4, longitude=1.2, **kwargs):
    return types.MessageMediaGeoLive(
        geo=types.GeoPoint(long=longitude, lat=latitude, access_hash=1, accuracy_radius=8),
        period=kwargs.pop("period", 3600),
        **kwargs,
    )


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


def test_live_movement_is_saved_quietly_and_deletion_uses_latest_coordinates(
    store, config, make_message
):
    original = make_message("", media=live_location())
    first = store.ingest(original, False, config, 1)
    expires = store.db.execute("SELECT expires FROM messages").fetchone()[0]
    moved = make_message("", media=live_location(3.5, 1.3, heading=180), edited=time.time())
    second = store.ingest(moved, True, config, 2)
    assert store.ingest(moved, True, config, 2) is None
    assert [v["media"]["latitude"] for v in store.history(first)] == [3.4, 3.5]
    assert store.stats()["pending_events"] == 0
    assert store.get("last_capture") == moved["observed_at"]
    assert store.db.execute("SELECT expires FROM messages").fetchone()[0] == expires

    assert store.deletions("common", [10], config, 1, 99) == 1
    event = store.pending_event()
    assert event["kind"] == "delete"
    assert event["before"]["version_id"] == second
    assert event["before"]["media"]["longitude"] == 1.3


def test_live_location_first_seen_mid_broadcast_is_a_quiet_baseline(store, config, make_message):
    first = store.ingest(
        make_message("", media=live_location(), edited=time.time()), True, config, 1
    )
    assert store.version(first)["media"]["label"] == "Live location"
    assert store.stats()["pending_events"] == 0
    assert store.deletions("common", [10], config, 1, 99) == 1


def test_live_location_old_snapshot_remains_quiet_after_upgrade(store, config, make_message):
    original = make_message("", media=live_location())
    original["media"]["label"] = "Location"
    for key in ("period", "heading", "proximity_notification_radius", "accuracy_radius"):
        original["media"].pop(key)
    first = store.ingest(original, False, config, 1)
    store.ingest(make_message("", media=live_location(3.5), edited=time.time()), True, config, 2)
    assert len(store.history(first)) == 2
    assert store.stats()["pending_events"] == 0


def test_live_heading_accuracy_and_period_changes_are_saved_without_alerts(
    store, config, make_message
):
    first = store.ingest(make_message("", media=live_location()), False, config, 1)
    # A new heading/accuracy or an extended broadcast need not move the point.
    media = live_location(heading=90, period=7200, proximity_notification_radius=100)
    media.geo.accuracy_radius = 20
    store.ingest(make_message("", media=media, edited=time.time()), True, config, 2)
    saved = store.history(first)
    assert len(saved) == 2
    assert saved[-1]["media"]["heading"] == 90
    assert saved[-1]["media"]["accuracy_radius"] == 20
    assert store.stats()["pending_events"] == 0


@pytest.mark.parametrize("change", ["text", "formatting", "static_location", "remove_location"])
def test_live_locations_do_not_hide_material_edits(store, config, make_message, change):
    store.ingest(make_message("Here", media=live_location()), False, config, 1)
    updated = make_message("Here", media=live_location(3.5), edited=time.time())
    if change == "text":
        updated["text"] = "Meet here"
    elif change == "formatting":
        updated["entities"] = [{"type": "bold", "offset": 0, "length": 4}]
    elif change == "static_location":
        updated = make_message(
            "Here",
            media=types.MessageMediaGeo(geo=types.GeoPoint(long=2, lat=4, access_hash=1)),
            edited=time.time(),
        )
    else:
        updated["media"] = None
    store.ingest(updated, True, config, 2)
    assert store.stats()["pending_events"] == 1
    assert store.pending_event()["after"]["text"] == updated["text"]


def test_static_location_edits_are_still_reported(store, config, make_message):
    for order, latitude in enumerate((3.4, 3.5), start=1):
        media = types.MessageMediaGeo(geo=types.GeoPoint(long=1.2, lat=latitude, access_hash=1))
        store.ingest(make_message("", media=media, edited=time.time()), order > 1, config, order)
    assert store.stats()["pending_events"] == 1
