from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from telethon import TelegramClient, utils

from . import __version__
from .config import (
    Config,
    credentials,
    default_config_path,
    load_config,
    private_directory,
    write_private,
)
from .daemon import Daemon
from .service import configure_logging, manage, wait_for_setup
from .store import Store


@contextmanager
def instance_lock(directory: Path):
    private_directory(directory)
    with (directory / "daemon.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Daemon is already running. Use the bot for live mode changes."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def init(path: Path):
    if path.exists():
        raise ValueError(f"Configuration already exists: {path}")
    template = files("telegram_daemon").joinpath("example.toml").read_text()
    template = template.replace('"__DEFAULT_DATA_DIR__"', json.dumps(str(Config().data_dir)))
    write_private(path, template)
    print(f"Created {path}\nDefault mode: personal (human direct messages, both directions).")


async def authenticate(config: Config):
    try:
        secret = credentials(config)
    except ValueError:
        api_id = int(
            (await asyncio.to_thread(input, "Telegram API ID (my.telegram.org): ")).strip()
        )
        api_hash = (await asyncio.to_thread(getpass.getpass, "Telegram API hash: ")).strip()
        bot_token = (
            await asyncio.to_thread(getpass.getpass, "Bot token from @BotFather: ")
        ).strip()
        if api_id <= 0 or not api_hash or ":" not in bot_token:
            raise ValueError("Invalid credentials; nothing saved") from None
        write_private(
            config.data_dir / "credentials.json",
            json.dumps(
                {
                    "api_id": api_id,
                    "api_hash": api_hash,
                    "bot_token": bot_token,
                }
            ),
        )
        secret = credentials(config)
    client = TelegramClient(str(config.data_dir / "user"), secret.api_id, secret.api_hash)
    owner = None
    try:
        await client.start()
        owner = await client.get_me()
    finally:
        await client.disconnect()
    write_private(config.data_dir / "auth.ready", str(owner.id))
    print(f"Authenticated account {owner.id}. An installed service will start automatically.")
    print("Open your bot and send /start. Without a service, run tgdaemon run first.")


async def sources(config: Config):
    secret = credentials(config)
    client = TelegramClient(str(config.data_dir / "user"), secret.api_id, secret.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Run tgdaemon auth first")
        async for dialog in client.iter_dialogs():
            print(f"{utils.get_peer_id(dialog.entity):>16}  {dialog.name}")
    finally:
        await client.disconnect()


def backup(config: Config, store: Store, destination: Path):
    if destination.exists():
        raise ValueError("Backup destination already exists; choose a new directory")
    private_directory(destination)
    store.backup(destination / "history.sqlite3")
    private_directory(destination / "media")
    manifest = []
    for name in sorted(store.media_paths()):
        source = config.data_dir / "media" / name
        if not source.is_file():
            raise ValueError("Referenced media is missing; backup is incomplete")
        shutil.copyfile(source, destination / "media" / name)
        manifest.append({"path": name, "sha256": Daemon.hash_file(source)})
    write_private(
        destination / "manifest.json",
        json.dumps(
            {
                "schema": 1,
                "version": __version__,
                "created": time.time(),
                "media": manifest,
            },
            indent=2,
        ),
    )
    print(f"History and media backed up to {destination}. Credentials/session are excluded.")


def restore(config: Config, source: Path):
    destination = config.data_dir / "history.sqlite3"
    if destination.exists():
        raise ValueError("Restore requires a data directory without an existing history database")
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest["schema"] != 1:
        raise ValueError("Unsupported backup schema")
    for item in manifest["media"]:
        name = item["path"]
        if Path(name).name != name or Daemon.hash_file(source / "media" / name) != item["sha256"]:
            raise ValueError("Backup media verification failed")
    with sqlite3.connect(f"file:{source / 'history.sqlite3'}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Backup database references are inconsistent")
    shutil.copyfile(source / "history.sqlite3", destination)
    private_directory(config.data_dir / "media")
    for item in manifest["media"]:
        shutil.copyfile(source / "media" / item["path"], config.data_dir / "media" / item["path"])
    store = Store(destination)
    try:
        store.recover()
        store.set("paused", True)
        store.set("bot_ready", False)
        store.set("running", False)
        # Old pending jobs may already have been delivered after the backup was taken.
        with store.lock, store.db:
            store.db.execute("UPDATE parts SET state='uncertain' WHERE state='pending'")
            store.db.execute("UPDATE events SET state='cancelled' WHERE state='pending'")
        print("Restored with capture/reports paused. Authenticate, run, then /start and /resume.")
        print("Old sends require /retry uncertain; already delivered messages may be duplicated.")
    finally:
        store.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Personal Telegram history daemon and private bot")
    root.add_argument("--config", type=Path, default=default_config_path())
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "auth", "run", "status", "sources", "doctor"):
        commands.add_parser(name)
    commands.add_parser("service-run", help=argparse.SUPPRESS)
    service = commands.add_parser("service", help="Manage the macOS background service")
    service.add_argument("action", choices=["install", "start", "stop", "restart", "status"])
    mode = commands.add_parser("mode", help="Set mode while the daemon is stopped")
    mode.add_argument("mode", choices=["personal", "selected", "all"])
    search = commands.add_parser("search")
    search.add_argument("query")
    history = commands.add_parser("history")
    history.add_argument("record", type=int)
    export = commands.add_parser(
        "export", help="Export a message's captured version history as JSON"
    )
    export.add_argument("record", type=int)
    for name in ("backup", "restore"):
        item = commands.add_parser(name)
        item.add_argument("path", type=Path)
    return root


def execute(args):
    if args.command == "init":
        init(args.config)
        return
    if not args.config.exists():
        raise ValueError("Configuration missing. Run tgdaemon init first.")
    config = load_config(args.config)
    private_directory(config.data_dir)
    if args.command == "service":
        manage(args.action, args.config, config)
        return
    if args.command == "service-run":
        configure_logging(config)
        wait_for_setup(config)
        args.command = "run"
    if args.command == "doctor":
        print(f"Python: {sys.version.split()[0]}\nSQLite: {sqlite3.sqlite_version}")
        print(f"Data directory: {config.data_dir}\nConfigured mode: {config.monitor.mode}")
        try:
            credentials(config)
            print("Credentials: configured (values hidden)")
        except ValueError:
            print("Credentials: missing; run tgdaemon auth")
        print(f"Free storage: {shutil.disk_usage(config.data_dir).free // 1048576} MiB")
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        print("FTS5: available")
        return
    if args.command in {"run", "auth", "sources", "mode", "backup", "restore"}:
        with instance_lock(config.data_dir):
            if args.command in {"auth", "sources"}:
                asyncio.run(authenticate(config) if args.command == "auth" else sources(config))
                return
            if args.command == "restore":
                restore(config, args.path)
                return
            store = Store(config.data_dir / "history.sqlite3")
            try:
                if args.command == "run":
                    asyncio.run(Daemon(config, credentials(config), store).run())
                elif args.command == "mode":
                    store.set("mode", args.mode)
                    print(f"Monitoring mode: {args.mode}")
                else:
                    backup(config, store, args.path)
            finally:
                store.close()
        return
    store = Store(config.data_dir / "history.sqlite3", read_only=True)
    try:
        if args.command == "status":
            result = store.stats()
            result["daemon_recently_alive"] = time.time() - (result["heartbeat"] or 0) < 45
            print(json.dumps(result, indent=2))
        elif args.command == "search":
            print(json.dumps(store.search(args.query), indent=2, ensure_ascii=False))
        else:
            print(json.dumps(store.history(args.record), indent=2, ensure_ascii=False))
    finally:
        store.close()


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP request logs include token-bearing URLs. Never enable them in normal operation.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        execute(parser().parse_args())
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Avoid accidentally printing an exception containing credentials or message content.
        print(
            f"Operation failed ({type(exc).__name__}). "
            "Run tgdaemon doctor and check configuration.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
