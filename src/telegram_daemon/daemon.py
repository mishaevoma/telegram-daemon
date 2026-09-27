from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import shutil
import signal
import tempfile
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from telethon import TelegramClient, errors, events

from .bot import AmbiguousDelivery, Bot, BotError, RetryableConnection
from .config import MODES, Config, Credentials, private_directory
from .content import media_info, snapshot
from .render import render_event
from .store import Store

log = logging.getLogger(__name__)
HELP = (
    "Telegram history\n\n"
    "/mode personal — human direct messages only\n"
    "/mode selected — only IDs listed in config\n"
    "/mode all — all chats (config exclusions still apply)\n"
    "/status — capture and delivery health\n"
    "/pause — pause capture and reports\n"
    "/resume — resume; paused intervals are coverage gaps\n"
    "/retry — retry failed or blocked reports\n"
    "/retry uncertain — explicitly retry ambiguous sends; duplicates are possible\n"
    "/help — show these commands\n\n"
    "Only your authenticated account can control this bot. Monitoring starts with new updates; "
    "messages never captured cannot be recovered."
)


class CaptureLimit(Exception):
    pass


class Daemon:
    def __init__(self, config: Config, secrets: Credentials, store: Store, *, user=None, bot=None):
        self.config = config.with_mode(store.get("mode", config.monitor.mode))
        self.store = store
        self.user = user or TelegramClient(
            str(config.data_dir / "user"),
            secrets.api_id,
            secrets.api_hash,
            sequential_updates=True,
            catch_up=True,
            flood_sleep_threshold=0,
            device_model="telegram-daemon",
            system_version="personal daemon",
        )
        self.bot = bot or Bot(secrets.bot_token)
        self.owner = 0
        self.bot_id = 0
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.capture_failure = False
        self.delivery_lock = asyncio.Lock()
        self.media_cache = OrderedDict()
        self.media_root = config.data_dir / "media"
        self.user.add_event_handler(self.new_message, events.NewMessage())
        self.user.add_event_handler(self.edited_message, events.MessageEdited())
        self.user.add_event_handler(self.deleted_message, events.MessageDeleted())

    async def db(self, name, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.store, name), *args, **kwargs)

    async def delay(self, seconds=1):
        with suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)

    async def new_message(self, event):
        await self.capture(event, False)

    async def edited_message(self, event):
        await self.capture(event, True)

    async def capture(self, event, edited: bool):
        await self.ready.wait()
        if self.stop.is_set() or self.store.get("paused", False):
            return
        try:
            chat = event.chat or await event.get_chat()
            # Never classify an unresolved private peer as a human.
            if event.is_private and chat is None:
                raise ValueError("Unresolved private peer")
            value = snapshot(event.message, chat, event.sender, self.owner)
            if value is None or not self.config.monitor.accepts(value, self.owner, self.bot_id):
                return
            if shutil.disk_usage(self.config.data_dir).free < self.config.min_free_mb * 1048576:
                raise OSError("Disk reserve reached")
            version_id = await self.db(
                "ingest", value, edited, self.config, getattr(event.original_update, "pts", 0)
            )
            if version_id and value.get("media"):
                self.media_cache[version_id] = event.message
                while len(self.media_cache) > 128:
                    self.media_cache.popitem(last=False)
        except Exception as exc:
            # Stop rather than silently consuming updates that cannot be durably recorded.
            log.error(
                "Capture failed (%s); stopping to expose the coverage gap", type(exc).__name__
            )
            self.capture_failure = True
            self.stop.set()

    async def deleted_message(self, event):
        await self.ready.wait()
        if self.stop.is_set() or self.store.get("paused", False):
            return
        channel = getattr(event.original_update, "channel_id", None)
        namespace = f"channel:{channel}" if channel else "common"
        try:
            await self.db(
                "deletions", namespace, event.deleted_ids, self.config, self.owner, self.bot_id
            )
        except Exception as exc:
            log.error("Deletion capture failed (%s)", type(exc).__name__)
            self.capture_failure = True
            self.stop.set()

    async def prepare_next(self):
        event = await self.db("pending_event")
        if not event:
            return
        value = event["after"] or event["before"]
        if not self.config.monitor.accepts(value, self.owner, self.bot_id):
            await self.db("cancel_event", event["id"])
            return
        waiting = any(
            (v.get("media") or {}).get("state") in {"queued", "downloading"}
            for v in (event["before"], event["after"])
            if v
        )
        if waiting and time.time() - event["observed"] < self.config.media_wait_seconds:
            return
        await self.db("prepare", event["id"], render_event(event, self.config.timezone))

    async def deliver_one(self) -> bool:
        async with self.delivery_lock:
            if self.store.get("paused", False) or not self.store.get("bot_ready", False):
                return False
            await self.prepare_next()
            part = await self.db("next_part")
            if not part:
                return False
            value = await self.db("event_message", part["event_id"])
            if value is None or not self.config.monitor.accepts(value, self.owner, self.bot_id):
                await self.db("cancel_event", part["event_id"])
                return True
            await self.db("part_state", part["id"], "sending")
            try:
                payload = dict(part["payload"])
                if part["position"] > 0:
                    payload["reply_to_id"] = await self.db("report_anchor", part["event_id"])
                result = await self.bot.send(self.owner, payload, self.media_root)
            except RetryableConnection:
                await self.db(
                    "part_state",
                    part["id"],
                    "pending",
                    error="connection",
                    retry_at=time.time() + min(300, 2 ** min(part["attempts"] + 1, 8)),
                )
            except AmbiguousDelivery:
                await self.db("part_state", part["id"], "uncertain", error="unconfirmed_response")
            except BotError as exc:
                await self.handle_send_error(part, exc)
            else:
                await self.db("part_state", part["id"], "sent", result_id=result["message_id"])
            return True

    async def handle_send_error(self, part: dict, exc: BotError):
        payload = dict(part["payload"])
        if exc.code == 429:
            until = time.time() + max(exc.retry_after, 1)
            await self.db("set", "bot_retry_at", until)
            await self.db("part_state", part["id"], "pending", retry_at=until, error="rate_limit")
        elif exc.code in {401, 403}:
            await self.db("part_state", part["id"], "blocked", error=f"bot_{exc.code}")
            await self.db("set", "bot_ready", False)
        elif exc.code == 400 and payload["kind"] == "text" and payload.get("entities"):
            payload["entities"] = []
            await self.db(
                "part_state", part["id"], "pending", payload=payload, error="formatting_fallback"
            )
        elif exc.code == 400 and payload["kind"] == "file":
            if payload["media_kind"] != "document":
                payload["media_kind"] = "document"
            else:
                payload = {
                    "kind": "text",
                    "text": part["payload"]["caption"]
                    + "\nTelegram could not accept this attachment. "
                    "The captured file remains local.",
                    "entities": [],
                }
            await self.db(
                "part_state", part["id"], "pending", payload=payload, error="media_fallback"
            )
        else:
            await self.db(
                "part_state",
                part["id"],
                "uncertain" if exc.code >= 500 else "failed",
                error=f"bot_{exc.code}",
            )

    async def delivery_loop(self):
        while not self.stop.is_set():
            until = self.store.get("bot_retry_at", 0)
            if until > time.time():
                await self.delay(min(until - time.time(), 5))
                continue
            sent = await self.deliver_one()
            await self.delay(1.1 if sent else 0.5)

    async def handle_command(self, update: dict):
        message = update.get("message", {})
        if (
            message.get("from", {}).get("id") != self.owner
            or message.get("chat", {}).get("id") != self.owner
            or message.get("chat", {}).get("type") != "private"
        ):
            return
        words = message.get("text", "").strip().split()
        if not words:
            return
        command = words[0].split("@")[0].lower()
        async with self.delivery_lock:
            if command in {"/start", "/help"}:
                if command == "/start":
                    await self.db("set", "bot_ready", True)
                    await self.db("retry")
                response = HELP + f"\n\nCurrent mode: {self.config.monitor.mode}."
            elif command == "/mode":
                if len(words) != 2 or words[1] not in MODES:
                    response = (
                        f"Current mode: {self.config.monitor.mode}. "
                        "Use /mode personal|selected|all."
                    )
                else:
                    self.config = self.config.with_mode(words[1])
                    await self.db("set", "mode", words[1])
                    response = (
                        f"Monitoring mode: {words[1]}. Applies to capture and queued reports."
                    )
                    if words[1] == "selected" and not self.config.monitor.chat_ids:
                        response += " No chat IDs are configured, so nothing will be captured."
            elif command in {"/pause", "/resume"}:
                paused = command == "/pause"
                await self.db("set", "paused", paused)
                await self.db("set", "last_pause_change", time.time())
                response = (
                    "Capture and reports paused. Messages during this interval may be missed."
                    if paused
                    else "Capture and reports resumed."
                )
            elif command == "/retry":
                uncertain = len(words) == 2 and words[1] == "uncertain"
                if len(words) > 1 and not uncertain:
                    response = (
                        "Use /retry or /retry uncertain (may duplicate a previously accepted send)."
                    )
                else:
                    count = await self.db("retry", uncertain)
                    response = f"Queued {count} parts for retry." + (
                        " Duplicates are possible." if uncertain else ""
                    )
            elif command == "/status":
                stats = await self.db("stats")
                states = stats["deliveries"]
                response = (
                    f"Mode: {self.config.monitor.mode}\nPaused: {stats['paused']}\n"
                    f"Telegram connected: {self.user.is_connected()}\n"
                    f"Bot polling conflict: {stats['bot_polling_conflict']}\n"
                    f"Retained: {stats['messages']} messages / {stats['versions']} versions\n"
                    f"Pending reports: {stats['pending_events']}\n"
                    f"Pending parts: {states.get('pending', 0)}\n"
                    f"Failed/blocked: {states.get('failed', 0) + states.get('blocked', 0)}\n"
                    f"Uncertain sends: {states.get('uncertain', 0)}\n"
                    f"Unclean starts: {stats['unclean_starts']}\n"
                    "Coverage is best-effort; missed Telegram updates cannot always be recovered."
                )
            else:
                response = "Use /help for commands."
            await self.bot.message(self.owner, response)

    async def bot_loop(self):
        offset = self.store.get("bot_offset", 0)
        while not self.stop.is_set():
            try:
                updates = await self.bot.call(
                    "getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]}
                )
                if self.store.get("bot_polling_conflict", False):
                    await self.db("set", "bot_polling_conflict", False)
                    log.info("Bot command polling recovered")
                for update in updates:
                    # Persist cursor only after applying the authorized command.
                    await self.handle_command(update)
                    offset = update["update_id"] + 1
                    await self.db("set", "bot_offset", offset)
            except BotError as exc:
                if exc.code == 401:
                    raise ValueError("Bot token invalid") from None
                if exc.code == 409:
                    if not self.store.get("bot_polling_conflict", False):
                        log.warning(
                            "Bot command polling conflicts with another poller or webhook; "
                            "capture and report delivery continue. Retrying in 60 seconds."
                        )
                    await self.db("set", "bot_polling_conflict", True)
                    await self.db("set", "last_bot_polling_conflict", time.time())
                    await self.delay(60)
                    continue
                await self.delay(max(2, min(exc.retry_after or 5, 60)))
            except (RetryableConnection, AmbiguousDelivery):
                await self.delay(3)

    async def media_loop(self):
        private_directory(self.media_root)
        while not self.stop.is_set():
            value = await self.db("media_job")
            if not value or self.store.get("paused", False):
                await self.delay(1)
                continue
            if not self.config.monitor.accepts(value, self.owner, self.bot_id):
                await self.db("media_result", value["version_id"], "skipped_scope")
                continue
            await self.download(value)

    async def download(self, value: dict):
        version_id = value["version_id"]
        media = value["media"]
        max_size = self.config.max_file_mb * 1048576
        used = sum(path.stat().st_size for path in self.media_root.iterdir() if path.is_file())
        budget_left = self.config.media_budget_mb * 1048576 - used
        if budget_left <= 0 or media.get("size", 0) > budget_left:
            await self.db("media_result", version_id, "skipped_budget")
            return
        await self.db("media_result", version_id, "downloading")
        fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=self.media_root)
        os.close(fd)
        temporary = Path(temporary)
        try:
            message = self.media_cache.pop(version_id, None)
            if message is None:
                message = await self.user.get_messages(value["chat_id"], ids=value["message_id"])
            actual = media_info(message) if message else None
            if not actual or actual.get("media_id") != media.get("media_id"):
                await self.db("media_result", version_id, "unavailable")
                return

            def progress(current, total):
                if current > min(max_size, budget_left) or total > min(max_size, budget_left):
                    raise CaptureLimit()
                if shutil.disk_usage(self.config.data_dir).free < self.config.min_free_mb * 1048576:
                    raise CaptureLimit()

            # A file object prevents Telethon from changing the output extension/path.
            with await asyncio.to_thread(temporary.open, "wb") as output:
                await self.user.download_media(message, file=output, progress_callback=progress)
            if temporary.stat().st_size == 0:
                await self.db("media_result", version_id, "unavailable")
                return
            digest = await asyncio.to_thread(self.hash_file, temporary)
            suffix = Path(media.get("filename", "")).suffix.lower()
            if not suffix or len(suffix) > 10 or not suffix[1:].isalnum():
                suffix = (
                    ".jpg"
                    if media["kind"] == "photo"
                    else mimetypes.guess_extension(media.get("mime", "")) or ".bin"
                )
            final = digest + suffix
            temporary.replace(self.media_root / final)
            await self.db("media_result", version_id, "captured", final)
        except errors.FloodWaitError as exc:
            await self.db("media_result", version_id, "queued")
            await self.delay(min(exc.seconds, 60))
            remaining = exc.seconds - 60
            while remaining > 0 and not self.stop.is_set():
                await self.delay(min(remaining, 60))
                remaining -= 60
        except CaptureLimit:
            await self.db(
                "media_result",
                version_id,
                "skipped_size" if max_size <= budget_left else "skipped_budget",
            )
        except (errors.RPCError, OSError, ValueError, TypeError):
            await self.db("media_result", version_id, "unavailable")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def hash_file(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    async def maintenance_loop(self):
        last_cleanup = 0
        connected = True
        while not self.stop.is_set():
            await self.db("set", "heartbeat", time.time())
            if connected and not self.user.is_connected():
                await self.db("set", "last_disconnect", time.time())
            connected = self.user.is_connected()
            if time.time() - last_cleanup >= 3600:
                async with self.delivery_lock:
                    await self.db("purge")
                    referenced = await self.db("media_paths")
                    for path in self.media_root.iterdir():
                        # Grace period protects a just-renamed file before its database commit.
                        if (
                            path.is_file()
                            and path.name not in referenced
                            and time.time() - path.stat().st_mtime > 3600
                        ):
                            path.unlink(missing_ok=True)
                last_cleanup = time.time()
            await self.delay(15)

    async def run(self):
        private_directory(self.config.data_dir)
        private_directory(self.media_root)
        self.store.recover()
        if self.store.get("running", False):
            self.store.set("unclean_starts", self.store.get("unclean_starts", 0) + 1)
        self.store.set("running", True)
        tasks = []
        clean = False
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        try:
            await self.user.connect()
            if not await self.user.is_user_authorized():
                raise ValueError("User session is not authenticated. Run tgdaemon auth first.")
            account = await self.user.get_me()
            self.owner = account.id
            await self.db("bind_account", self.owner)
            identity = await self.bot.call("getMe")
            self.bot_id = identity["id"]
            if self.store.get("bot_id") != self.bot_id:
                await self.db("set", "bot_id", self.bot_id)
                await self.db("set", "bot_ready", False)
                await self.db("set", "bot_offset", 0)
            await self.db("set", "mode", self.config.monitor.mode)
            self.ready.set()
            await self.user.catch_up()
            log.info(
                "Daemon active in %s mode. Open @%s and send /start.",
                self.config.monitor.mode,
                identity["username"],
            )
            tasks = [
                asyncio.create_task(worker())
                for worker in (
                    self.delivery_loop,
                    self.media_loop,
                    self.bot_loop,
                    self.maintenance_loop,
                )
            ]
            stopper = asyncio.create_task(self.stop.wait())
            disconnected = asyncio.ensure_future(self.user.disconnected)
            done, _ = await asyncio.wait(
                [*tasks, stopper, disconnected], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task in tasks:
                    task.result()
                    if not self.stop.is_set():
                        raise RuntimeError("A background worker stopped unexpectedly")
            if self.capture_failure:
                raise RuntimeError("Capture failed; an incomplete interval was recorded")
            if disconnected in done:
                raise RuntimeError("Telegram disconnected permanently")
            clean = self.stop.is_set()
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            # Do not cancel Telethon's shared disconnection future.
        finally:
            self.stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.user.disconnect()
            await self.bot.close()
            self.store.set("running", not clean)
            self.store.set("last_disconnect", time.time())
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
