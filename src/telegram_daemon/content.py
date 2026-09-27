"""Normalize Telegram objects into inert JSON and Bot API text entities."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from telethon import types, utils

ENTITY_TYPES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityUrl": "url",
    "MessageEntityEmail": "email",
    "MessageEntityMention": "mention",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityPhone": "phone_number",
    "MessageEntityBotCommand": "bot_command",
}


def utf16len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def text_value(value) -> str:
    if isinstance(value, str):
        return value
    return getattr(value, "text", "") or ""


def entities_to_json(entities, text: str) -> list[dict]:
    result = []
    boundaries = {0}
    size = 0
    for char in text:
        size += utf16len(char)
        boundaries.add(size)
    for entity in entities or []:
        name = type(entity).__name__
        kind = ENTITY_TYPES.get(name)
        if name == "MessageEntityBlockquote":
            kind = "expandable_blockquote" if getattr(entity, "collapsed", False) else "blockquote"
        if name in {"MessageEntityMentionName", "InputMessageEntityMentionName"}:
            kind = "text_link"
        # Custom emoji fall back to their actual Unicode text; unknown types stay readable.
        if not kind:
            continue
        start, length = entity.offset, entity.length
        if length <= 0 or start not in boundaries or start + length not in boundaries:
            continue
        item = {"type": kind, "offset": start, "length": length}
        if kind == "text_link":
            user_id = getattr(entity, "user_id", None)
            if user_id is not None:
                user_id = getattr(user_id, "user_id", user_id)
                item["url"] = f"tg://user?id={user_id}"
            else:
                item["url"] = entity.url
            if not item["url"].startswith(("https://", "http://", "tg://", "mailto:")):
                continue
        if kind == "pre" and getattr(entity, "language", ""):
            item["language"] = entity.language
        result.append(item)
    return sorted(result, key=lambda item: (item["offset"], -item["length"]))


def media_info(message) -> dict | None:
    media = getattr(message, "media", None)
    if media is None or isinstance(media, (types.MessageMediaEmpty, types.MessageMediaWebPage)):
        return None
    name = type(media).__name__
    result = {
        "kind": "unsupported",
        "label": "Attachment (preview unavailable)",
        "tg_type": name,
        "state": "metadata_only",
    }
    if isinstance(media, types.MessageMediaPhoto):
        photo = media.photo
        result.update(kind="photo", label="Photo", media_id=getattr(photo, "id", None))
        sizes = getattr(photo, "sizes", [])
        result["size"] = max(
            [getattr(size, "size", 0) or max(getattr(size, "sizes", []) or [0]) for size in sizes]
            or [0]
        )
    elif isinstance(media, types.MessageMediaDocument):
        doc = media.document
        if not isinstance(doc, types.Document):
            return result
        result.update(
            kind="document", label="File", media_id=doc.id, size=doc.size, mime=doc.mime_type
        )
        for attr in doc.attributes:
            if isinstance(attr, types.DocumentAttributeFilename):
                result["filename"] = attr.file_name
            elif isinstance(attr, types.DocumentAttributeAudio):
                result.update(
                    kind="voice" if attr.voice else "audio",
                    label="Voice message" if attr.voice else "Audio",
                    duration=attr.duration,
                )
            elif isinstance(attr, types.DocumentAttributeVideo):
                result.update(
                    kind="video_note" if attr.round_message else "video",
                    label="Video message" if attr.round_message else "Video",
                    duration=attr.duration,
                )
            elif isinstance(attr, types.DocumentAttributeSticker):
                result.update(kind="sticker", label=f"Sticker {attr.alt or ''}".strip())
        sticker = next(
            (a for a in doc.attributes if isinstance(a, types.DocumentAttributeSticker)), None
        )
        if sticker is not None:
            # Video/animated stickers carry multiple attributes in no guaranteed order.
            result.update(kind="sticker", label=f"Sticker {sticker.alt or ''}".strip())
        elif any(isinstance(a, types.DocumentAttributeAnimated) for a in doc.attributes):
            result.update(kind="animation", label="Animation")
    elif isinstance(media, types.MessageMediaContact):
        result.update(
            kind="contact",
            label="Contact",
            first_name=media.first_name,
            last_name=media.last_name,
            phone=media.phone_number,
        )
    elif isinstance(
        media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)
    ):
        geo = media.geo
        result.update(
            kind="location",
            label="Location",
            latitude=getattr(geo, "lat", None),
            longitude=getattr(geo, "long", None),
        )
        if isinstance(media, types.MessageMediaVenue):
            result.update(label="Venue", title=media.title, address=media.address)
        elif isinstance(media, types.MessageMediaGeoLive):
            result.update(
                label="Live location",
                period=media.period,
                heading=media.heading,
                proximity_notification_radius=media.proximity_notification_radius,
                accuracy_radius=getattr(geo, "accuracy_radius", None),
            )
    elif isinstance(media, types.MessageMediaPoll):
        result.update(
            kind="poll",
            label="Poll",
            media_id=media.poll.id,
            question=text_value(media.poll.question),
            answers=[text_value(answer.text) for answer in media.poll.answers],
        )
    elif isinstance(media, types.MessageMediaDice):
        result.update(kind="dice", label=f"{media.emoticon} Result: {media.value}")
    else:
        labels = {
            "MessageMediaGame": "Game",
            "MessageMediaInvoice": "Invoice",
            "MessageMediaStory": "Story",
            "MessageMediaPaidMedia": "Paid media",
            "MessageMediaUnsupported": "Unsupported attachment",
        }
        result["label"] = labels.get(name, result["label"])
    result["spoiler"] = bool(getattr(media, "spoiler", False))
    if getattr(media, "ttl_seconds", None):
        result.update(state="ephemeral", label="Expiring media (not archived)")
    elif getattr(message, "noforwards", False):
        result["state"] = "protected"
    return result


def display_name(entity, fallback: str) -> str:
    if entity is None:
        return fallback
    return (
        getattr(entity, "title", None)
        or " ".join(
            filter(None, (getattr(entity, "first_name", None), getattr(entity, "last_name", None)))
        )
        or getattr(entity, "username", None)
        or fallback
    )


def timestamp(value) -> float | None:
    return value.replace(tzinfo=value.tzinfo or UTC).timestamp() if value else None


def snapshot(message, chat, sender, account_id: int, now: float | None = None) -> dict | None:
    if (
        not isinstance(message, types.Message)
        or message.peer_id is None
        or getattr(message, "action", None) is not None
    ):
        return None
    peer = message.peer_id
    chat_id = utils.get_peer_id(peer)
    channel_id = peer.channel_id if isinstance(peer, types.PeerChannel) else None
    kind = "private" if isinstance(peer, types.PeerUser) else "group"
    if channel_id and getattr(chat, "broadcast", False):
        kind = "channel"
    username = getattr(chat, "username", None)
    link = None
    if channel_id:
        link = (
            f"https://t.me/{username}/{message.id}"
            if username
            else f"https://t.me/c/{channel_id}/{message.id}"
        )
    text = (message.message or "").encode("utf-8", "replace").decode("utf-8")
    if sender is None and kind == "private" and not message.out:
        sender = chat
    media = media_info(message)
    if getattr(message, "rich_message", None) and not text and not media:
        media = {
            "kind": "unsupported",
            "label": "Rich message (preview unavailable)",
            "tg_type": "RichMessage",
            "state": "metadata_only",
        }
    return {
        "account_id": account_id,
        "namespace": f"channel:{channel_id}" if channel_id else "common",
        "message_id": message.id,
        "chat_id": chat_id,
        "chat_kind": kind,
        "chat_name": display_name(chat, f"Chat {chat_id}"),
        "sender_name": display_name(sender, "Unknown sender"),
        "sender_id": utils.get_peer_id(message.from_id) if message.from_id else chat_id,
        "is_bot": bool(getattr(chat, "bot", False)),
        "outgoing": bool(message.out),
        "text": text,
        "entities": entities_to_json(message.entities, text),
        "media": media,
        "sent_at": timestamp(message.date),
        "edit_at": timestamp(message.edit_date),
        "observed_at": now or datetime.now(UTC).timestamp(),
        "source_link": link,
        "album_id": message.grouped_id,
        "reply_to_id": getattr(message.reply_to, "reply_to_msg_id", None),
    }


def fingerprint(message: dict) -> str:
    media = dict(message.get("media") or {})
    for key in ("state", "path", "size", "error"):
        media.pop(key, None)
    value = {"text": message["text"], "entities": message["entities"], "media": media}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def is_live_location_update(before: dict | None, after: dict) -> bool:
    """Recognize telemetry edits without discarding their coordinates from history."""
    current = after.get("media") or {}
    if current.get("tg_type") != "MessageMediaGeoLive":
        return False
    if before is None:
        # An empty live location first seen mid-broadcast is a baseline, not a text edit.
        return not after["text"] and not after["entities"]
    previous = before.get("media") or {}
    if previous.get("tg_type") != "MessageMediaGeoLive":
        return False
    if before["text"] != after["text"] or before["entities"] != after["entities"]:
        return False
    ignored = {
        "latitude",
        "longitude",
        "period",
        "heading",
        "proximity_notification_radius",
        "accuracy_radius",
        # Older snapshots used "Location" and omitted the live metadata above.
        "label",
        "state",
        "path",
        "size",
        "error",
    }
    return {k: v for k, v in previous.items() if k not in ignored} == {
        k: v for k, v in current.items() if k not in ignored
    }
