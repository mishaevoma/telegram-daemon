from datetime import UTC, datetime

import pytest
from telethon import types

from telegram_daemon.content import entities_to_json, media_info, snapshot, utf16len
from telegram_daemon.render import RichText, media_description, render_event


def test_utf16_entities_survive_chunking_and_untrusted_markup():
    text = "😀 <b>not HTML</b> " + "🌍" * 2100 + "& end"
    source = types.MessageEntityBold(offset=3, length=utf16len(text) - 3)
    entities = entities_to_json([source], text)
    rich = RichText().add("Before\n", "bold").add(text, entities=entities)
    chunks = rich.chunks()
    assert "".join(chunk["text"] for chunk in chunks) == "Before\n" + text
    assert len(chunks) == 2
    for chunk in chunks:
        assert utf16len(chunk["text"]) <= 3500
        boundaries = {utf16len(chunk["text"][:i]) for i in range(len(chunk["text"]) + 1)}
        for entity in chunk["entities"]:
            assert entity["offset"] in boundaries
            assert entity["offset"] + entity["length"] in boundaries


def test_unknown_custom_emoji_and_bad_offsets_keep_plain_text():
    text = "😀hello"
    entities = entities_to_json(
        [
            types.MessageEntityCustomEmoji(offset=0, length=2, document_id=123),
            types.MessageEntityUnknown(offset=2, length=5),
            types.MessageEntityItalic(offset=1, length=3),
            types.MessageEntityBold(offset=2, length=5),
        ],
        text,
    )
    assert entities == [{"type": "bold", "offset": 2, "length": 5}]


def test_nested_code_quote_links_and_mentions():
    text = "Code\nlink\nAlex"
    result = entities_to_json(
        [
            types.MessageEntityPre(offset=0, length=4, language="python"),
            types.MessageEntityTextUrl(offset=5, length=4, url="https://example.org?a=1&b=2"),
            types.MessageEntityMentionName(offset=10, length=4, user_id=42),
            types.MessageEntityBlockquote(offset=0, length=4, collapsed=True),
        ],
        text,
    )
    assert any(e.get("language") == "python" for e in result)
    assert any(e["type"] == "expandable_blockquote" for e in result)
    assert any(e.get("url") == "tg://user?id=42" for e in result)


@pytest.mark.parametrize(
    "attribute,kind",
    [
        (types.DocumentAttributeAudio(duration=12, voice=True), "voice"),
        (types.DocumentAttributeAudio(duration=12, title="Song"), "audio"),
        (types.DocumentAttributeVideo(duration=2, w=10, h=10), "video"),
        (types.DocumentAttributeVideo(duration=2, w=10, h=10, round_message=True), "video_note"),
        (
            types.DocumentAttributeSticker(alt="😎", stickerset=types.InputStickerSetEmpty()),
            "sticker",
        ),
        (types.DocumentAttributeAnimated(), "animation"),
        (types.DocumentAttributeFilename(file_name="../../sample.pdf"), "document"),
    ],
)
def test_documents_are_classified_without_serializing_library_objects(attribute, kind):
    doc = types.Document(
        id=9,
        access_hash=12,
        file_reference=b"ref",
        date=datetime.now(UTC),
        mime_type="application/octet-stream",
        size=512,
        dc_id=2,
        attributes=[attribute],
    )
    message = types.Message(
        id=1, peer_id=types.PeerUser(2), media=types.MessageMediaDocument(document=doc)
    )
    info = media_info(message)
    assert info["kind"] == kind
    assert info["media_id"] == 9
    assert "ref" not in str(info)


def test_poll_contact_location_dice_and_unknown_objects():
    media = [
        types.MessageMediaContact(
            phone_number="123", first_name="Alex", last_name="", vcard="", user_id=2
        ),
        types.MessageMediaGeo(geo=types.GeoPoint(long=1.2, lat=3.4, access_hash=1)),
        types.MessageMediaDice(value=4, emoticon="🎲"),
        types.MessageMediaUnsupported(),
        types.MessageMediaPoll(
            poll=types.Poll(
                id=4,
                question=types.TextWithEntities("Lunch?", []),
                hash=1,
                answers=[types.PollAnswer(types.TextWithEntities("Yes", []), b"yes")],
            ),
            results=types.PollResults(),
        ),
    ]
    for attachment, expected in zip(
        media, ("contact", "location", "dice", "unsupported", "poll"), strict=True
    ):
        value = media_info(types.Message(id=1, peer_id=types.PeerUser(2), media=attachment))
        assert value["kind"] == expected
        assert media_description(value)


def test_web_previews_are_not_material_edits_and_service_messages_are_ignored():
    message = types.Message(
        id=1, peer_id=types.PeerUser(2), media=types.MessageMediaWebPage(types.WebPageEmpty(id=5))
    )
    assert media_info(message) is None
    service = types.MessageService(
        id=1, peer_id=types.PeerUser(2), date=datetime.now(UTC), action=types.MessageActionEmpty()
    )
    assert snapshot(service, None, None, 1) is None


def test_reports_explain_missing_original_and_keep_formatting(make_message):
    value = make_message("😀 updated", entities=[types.MessageEntitySpoiler(3, 7)])
    value["version_id"] = 12
    report = render_event(
        {"id": 1, "kind": "edit", "before": None, "after": value, "observed": value["observed_at"]},
        "Europe/Lisbon",
    )
    text = report[0]["text"]
    assert "Earlier version was not captured" in text
    assert "Before" in text and "After" in text and "Alex" in text
    spoiler = next(e for e in report[0]["entities"] if e["type"] == "spoiler")
    raw = text.encode("utf-16-le")
    assert (
        raw[spoiler["offset"] * 2 : (spoiler["offset"] + spoiler["length"]) * 2].decode("utf-16-le")
        == "updated"
    )
