from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .content import utf16len


class RichText:
    def __init__(self):
        self.text = ""
        self.entities = []

    def add(self, text: str, style: str | None = None, entities=(), **attributes):
        offset = utf16len(self.text)
        self.text += text
        if style and text:
            self.entities.append(
                {"type": style, "offset": offset, "length": utf16len(text), **attributes}
            )
        self.entities.extend({**entity, "offset": offset + entity["offset"]} for entity in entities)
        return self

    def chunks(self, limit=3500) -> list[dict]:
        """Split on Unicode code points; Bot API entity offsets are UTF-16 code units."""
        result = []
        start_index = 0
        start_units = 0
        while start_index < len(self.text):
            end_index, units = start_index, 0
            while end_index < len(self.text):
                width = utf16len(self.text[end_index])
                if units + width > limit:
                    break
                units += width
                end_index += 1
            if end_index == start_index:
                raise ValueError("Chunk limit is too small for one character")
            text = self.text[start_index:end_index]
            entities = []
            end_units = start_units + units
            for entity in self.entities:
                left = max(start_units, entity["offset"])
                right = min(end_units, entity["offset"] + entity["length"])
                if right <= left:
                    continue
                # Automatic entities need a whole token; styles and explicit links can be clipped.
                if entity["type"] in {
                    "url",
                    "email",
                    "mention",
                    "hashtag",
                    "cashtag",
                    "phone_number",
                    "bot_command",
                }:
                    if left != entity["offset"] or right != entity["offset"] + entity["length"]:
                        continue
                entities.append({**entity, "offset": left - start_units, "length": right - left})
            result.append({"kind": "text", "text": text, "entities": entities})
            start_index, start_units = end_index, end_units
        return result


def media_description(media: dict | None) -> str:
    if not media:
        return ""
    lines = [media["label"]]
    kind = media["kind"]
    if kind == "contact":
        lines += [
            " ".join(filter(None, (media.get("first_name"), media.get("last_name")))),
            media.get("phone", ""),
        ]
    elif kind == "location":
        lines += [
            media.get("title", ""),
            media.get("address", ""),
            f"{media.get('latitude')}, {media.get('longitude')}",
        ]
    elif kind == "poll":
        lines += [media["question"], *(f"• {answer}" for answer in media["answers"])]
    else:
        if media.get("filename"):
            lines.append(media["filename"])
        if media.get("duration"):
            lines.append(f"{media['duration']:g} seconds")
        if media.get("size"):
            lines.append(f"{media['size'] / 1048576:.1f} MiB")
    reasons = {
        "metadata_only": "Attachment bytes were not saved",
        "queued": "Attachment is still being saved; available locally if capture succeeds",
        "downloading": "Attachment is still being saved; available locally if capture succeeds",
        "skipped_size": "Attachment exceeded the capture size limit",
        "skipped_budget": "Attachment exceeded the storage budget",
        "unavailable": "Attachment could not be captured before access was lost",
        "protected": "Protected attachment; metadata only",
        "ephemeral": "Expiring attachment; metadata only",
        "skipped_scope": "Attachment capture stopped after a monitoring mode change",
    }
    if media.get("media_id") and kind != "poll" and media["state"] != "captured":
        lines.append(reasons.get(media["state"], "Attachment unavailable"))
    if media.get("spoiler"):
        lines.append("Spoiler")
    return (
        " · ".join(filter(None, lines))
        if kind not in {"poll", "contact"}
        else "\n".join(filter(None, lines))
    )


def render_event(event: dict, timezone: str) -> list[dict]:
    before, after = event["before"], event["after"]
    current = after or before
    zone = ZoneInfo(timezone)

    def date(value):
        return (
            datetime.fromtimestamp(value, zone).strftime("%d %b %Y, %H:%M:%S %Z")
            if value
            else "Unknown"
        )

    text = RichText()
    text.add("✏️ Message edited\n" if event["kind"] == "edit" else "🗑 Message deleted\n", "bold")
    text.add(current["chat_name"] + "\n", "bold")
    text.add(f"From: {current['sender_name']}\n")
    text.add(f"Sent: {date(current.get('sent_at'))}\n")
    text.add(f"Observed: {date(event['observed'])}\n")
    if current.get("source_link"):
        text.add("Open source", "text_link", url=current["source_link"]).add("\n")
    if current.get("reply_to_id"):
        text.add(f"Reply to message {current['reply_to_id']}\n")
    if current.get("album_id"):
        text.add("Part of an album\n")

    def body(value, label):
        text.add(f"\n{label}\n", "bold")
        if value is None:
            text.add("Earlier version was not captured.\n")
            return
        if value["text"]:
            text.add(value["text"], entities=value["entities"]).add("\n")
        description = media_description(value.get("media"))
        if description:
            text.add(description + "\n")
        if not value["text"] and not description:
            text.add("Empty message\n")

    if event["kind"] == "edit":
        body(before, "Before")
        body(after, "After")
    else:
        body(before, "Last captured version")
    text.add(f"\nRecord #{current['version_id']} · Event #{event['id']}", "italic")
    parts = text.chunks()
    seen_paths = set()
    for value, label in ((before, "Before"), (after, "After")):
        media = value.get("media") if value else None
        if not media or media.get("state") != "captured" or not media.get("path"):
            continue
        if media["path"] in seen_paths:
            continue
        seen_paths.add(media["path"])
        parts.append(
            {
                "kind": "file",
                "media_kind": media["kind"],
                "path": media["path"],
                "filename": media.get("filename"),
                "spoiler": media.get("spoiler", False),
                "caption": (
                    f"{label if after else 'Recovered attachment'} · Record #{value['version_id']}"
                ),
            }
        )
    return parts
