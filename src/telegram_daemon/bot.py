from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx


class BotError(Exception):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        # Do not include request URLs (which contain the bot token) in exceptions/logs.
        super().__init__(f"Telegram Bot API error {code}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class AmbiguousDelivery(Exception):
    """The request may have been accepted; retrying might duplicate it."""


class RetryableConnection(Exception):
    pass


class Bot:
    def __init__(self, token: str, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=httpx.Timeout(60, connect=15),
            transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def call(self, method: str, data: dict | None = None, files=None):
        try:
            if files:
                fields = {
                    key: json.dumps(value) if not isinstance(value, str) else value
                    for key, value in (data or {}).items()
                }
                response = await self.client.post(method, data=fields, files=files)
            else:
                response = await self.client.post(method, json=data or {})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise RetryableConnection("Could not connect to Telegram") from None
        except httpx.HTTPError:
            raise AmbiguousDelivery("Telegram response was not confirmed") from None
        try:
            body = response.json()
        except (ValueError, TypeError):
            raise AmbiguousDelivery("Telegram returned an unreadable response") from None
        if not isinstance(body, dict) or "ok" not in body:
            raise AmbiguousDelivery("Telegram returned an unexpected response")
        if not body["ok"]:
            raise BotError(
                body.get("error_code", response.status_code),
                body.get("description", ""),
                body.get("parameters", {}).get("retry_after", 0),
            )
        return body["result"]

    async def message(self, owner: int, text: str):
        return await self.call(
            "sendMessage",
            {"chat_id": owner, "text": text, "link_preview_options": {"is_disabled": True}},
        )

    async def send(self, owner: int, payload: dict, media_root: Path):
        reply = {}
        if payload.get("reply_to_id"):
            reply["reply_parameters"] = {
                "message_id": payload["reply_to_id"],
                "allow_sending_without_reply": True,
            }
        if payload["kind"] == "text":
            return await self.call(
                "sendMessage",
                {
                    "chat_id": owner,
                    "text": payload["text"],
                    "entities": payload.get("entities", []),
                    "link_preview_options": {"is_disabled": True},
                    **reply,
                },
            )
        root = await asyncio.to_thread(media_root.resolve)
        path = await asyncio.to_thread((root / payload["path"]).resolve)
        if not path.is_relative_to(root) or not path.is_file():
            return await self.message(
                owner, payload["caption"] + "\nAttachment no longer available locally."
            )
        kinds = {
            "photo": ("sendPhoto", "photo"),
            "video": ("sendVideo", "video"),
            "voice": ("sendVoice", "voice"),
            "audio": ("sendAudio", "audio"),
            "animation": ("sendAnimation", "animation"),
            "sticker": ("sendSticker", "sticker"),
            "video_note": ("sendVideoNote", "video_note"),
        }
        method, field = kinds.get(payload["media_kind"], ("sendDocument", "document"))
        data = {"chat_id": owner, **reply}
        if field not in {"sticker", "video_note"}:
            data["caption"] = payload["caption"]
        if payload.get("spoiler") and field in {"photo", "video", "animation"}:
            data["has_spoiler"] = True
        filename = Path(payload.get("filename") or path.name).name
        with path.open("rb") as stream:
            return await self.call(method, data, files={field: (filename, stream)})
