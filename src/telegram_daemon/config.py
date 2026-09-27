from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_config_path, user_data_path

MODES = {"personal", "selected", "all"}


def default_config_path() -> Path:
    return user_config_path("telegram-daemon") / "config.toml"


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private(path: Path, text: str) -> None:
    private_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)
    path.chmod(0o600)


@dataclass(frozen=True)
class Monitor:
    mode: str = "personal"
    chat_ids: tuple[int, ...] = ()
    exclude_chat_ids: tuple[int, ...] = (777000,)
    include_outgoing: bool = True
    include_bots: bool = False
    notify_edits: bool = True
    notify_deletions: bool = True

    def accepts(self, message: dict, owner_id: int, bot_id: int = 0) -> bool:
        peer = message["chat_id"]
        if peer in {owner_id, bot_id, *self.exclude_chat_ids}:
            return False
        if message.get("outgoing") and not self.include_outgoing:
            return False
        if message.get("is_bot") and not self.include_bots:
            return False
        if self.mode == "personal":
            return message["chat_kind"] == "private" and not message.get("is_bot")
        return self.mode == "all" or peer in self.chat_ids


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=lambda: user_data_path("telegram-daemon"))
    timezone: str = "UTC"
    monitor: Monitor = field(default_factory=Monitor)
    cache_days: int = 7
    changed_days: int = 90
    min_free_mb: int = 256
    download_media: bool = True
    max_file_mb: int = 20
    media_budget_mb: int = 2048
    media_wait_seconds: int = 15

    def with_mode(self, mode: str) -> Config:
        if mode not in MODES:
            raise ValueError("Mode must be personal, selected, or all")
        return replace(self, monitor=replace(self.monitor, mode=mode))


def load_config(path: Path) -> Config:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    allowed = {"data_dir", "timezone", "monitor", "storage", "media", "bot"}
    if raw.keys() - allowed:
        raise ValueError(f"Unknown configuration fields: {sorted(raw.keys() - allowed)}")
    monitor = dict(raw.get("monitor", {}))
    for name in ("chat_ids", "exclude_chat_ids"):
        if name in monitor:
            if not isinstance(monitor[name], list) or any(
                type(value) is not int or value == 0 for value in monitor[name]
            ):
                raise ValueError(f"{name} must be a list of non-zero numeric Telegram peer IDs")
            monitor[name] = tuple(monitor[name])
    try:
        policy = Monitor(**monitor)
    except TypeError as exc:
        raise ValueError("Unknown monitor setting") from exc
    if policy.mode not in MODES:
        raise ValueError("monitor.mode must be personal, selected, or all")
    for name in ("include_outgoing", "include_bots", "notify_edits", "notify_deletions"):
        if type(getattr(policy, name)) is not bool:
            raise ValueError(f"monitor.{name} must be boolean")
    values = {}
    mappings = {
        "storage": {
            "cache_days": "cache_days",
            "changed_days": "changed_days",
            "min_free_mb": "min_free_mb",
        },
        "media": {
            "download": "download_media",
            "max_file_mb": "max_file_mb",
            "budget_mb": "media_budget_mb",
        },
        "bot": {"media_wait_seconds": "media_wait_seconds"},
    }
    for section, mapping in mappings.items():
        for key, value in raw.get(section, {}).items():
            if key not in mapping:
                raise ValueError(f"Unknown {section} setting: {key}")
            if key == "download":
                if type(value) is not bool:
                    raise ValueError("media.download must be boolean")
            elif type(value) is not int or value < 1:
                raise ValueError(f"{section}.{key} must be a positive integer")
            values[mapping[key]] = value
    timezone = raw.get("timezone", "UTC")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError("timezone must be an IANA timezone, such as Europe/Lisbon") from exc
    data_dir = Path(raw.get("data_dir", user_data_path("telegram-daemon"))).expanduser()
    if not data_dir.is_absolute():
        data_dir = path.resolve().parent / data_dir
    return Config(data_dir=data_dir, timezone=timezone, monitor=policy, **values)


@dataclass(frozen=True)
class Credentials:
    api_id: int
    api_hash: str = field(repr=False)
    bot_token: str = field(repr=False)


def credentials(config: Config) -> Credentials:
    path = config.data_dir / "credentials.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    api_id = os.environ.get("TG_API_ID", saved.get("api_id"))
    api_hash = os.environ.get("TG_API_HASH", saved.get("api_hash"))
    bot_token = os.environ.get("TG_BOT_TOKEN", saved.get("bot_token"))
    if not api_id or not api_hash or not bot_token:
        raise ValueError("Credentials missing. Run tgdaemon auth, or set TG_API_ID/HASH/BOT_TOKEN.")
    if int(api_id) <= 0 or ":" not in bot_token:
        raise ValueError("Invalid Telegram API ID or bot token")
    return Credentials(int(api_id), api_hash, bot_token)
