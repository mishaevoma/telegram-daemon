import json

import httpx
import pytest

from telegram_daemon.bot import AmbiguousDelivery, Bot, BotError


async def test_bot_uses_entities_not_markup_parser(tmp_path):
    def transport(request):
        value = json.loads(request.content)
        assert "parse_mode" not in value
        assert value["text"] == "<b>literal</b> 😀"
        assert value["entities"][0]["type"] == "bold"
        assert value["link_preview_options"]["is_disabled"]
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    bot = Bot("123:secret", transport=httpx.MockTransport(transport))
    try:
        await bot.send(
            1,
            {
                "kind": "text",
                "text": "<b>literal</b> 😀",
                "entities": [{"type": "bold", "offset": 0, "length": 3}],
            },
            tmp_path,
        )
    finally:
        await bot.close()


async def test_timeout_is_ambiguous_without_leaking_token():
    def transport(request):
        raise httpx.ReadTimeout(f"request timed out: {request.url}")

    bot = Bot("123:secret", transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(AmbiguousDelivery) as caught:
            await bot.message(1, "hello")
        assert "secret" not in str(caught.value)
        assert caught.value.__suppress_context__
    finally:
        await bot.close()


async def test_rate_limit_is_a_definitive_rejection():
    bot = Bot(
        "123:secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 17},
                },
            )
        ),
    )
    try:
        with pytest.raises(BotError) as caught:
            await bot.message(1, "hello")
        assert caught.value.retry_after == 17
    finally:
        await bot.close()


async def test_attachment_upload_uses_the_correct_bot_endpoint(tmp_path):
    (tmp_path / "capture.ogg").write_bytes(b"example")

    def transport(request):
        assert request.url.path.endswith("/sendVoice")
        assert b'name="voice"' in request.content
        assert b'filename="capture.ogg"' in request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    bot = Bot("123:secret", transport=httpx.MockTransport(transport))
    try:
        await bot.send(
            1,
            {"kind": "file", "path": "capture.ogg", "media_kind": "voice", "caption": "Recovered"},
            tmp_path,
        )
    finally:
        await bot.close()
