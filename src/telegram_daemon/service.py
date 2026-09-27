"""User-owned macOS launchd installation; no credentials in the property list."""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, credentials, private_directory, write_private

LABEL = "io.github.mishaevoma.telegram-daemon"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def launchctl(*arguments: str, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["/bin/launchctl", *arguments], capture_output=True, text=True, timeout=45
    )
    if check and result.returncode:
        raise ValueError(f"launchctl {arguments[0]} failed: {result.stderr.strip()}")
    return result


def build_plist(config_path: Path, config: Config, python: str) -> dict:
    # Keep the virtualenv interpreter path: resolving its symlink would lose the environment.
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(Path(python).absolute()),
            "-m",
            "telegram_daemon.cli",
            "--config",
            str(config_path.resolve()),
            "service-run",
        ],
        "WorkingDirectory": str(config.data_dir.resolve()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ExitTimeOut": 30,
        "ProcessType": "Background",
        "Umask": 0o077,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(config.data_dir / "logs" / "bootstrap.log"),
        "StandardErrorPath": str(config.data_dir / "logs" / "bootstrap.log"),
    }


def ready_for_capture(config: Config) -> bool:
    if not (config.data_dir / "auth.ready").is_file():
        return False
    if not (config.data_dir / "user.session").is_file():
        return False
    try:
        credentials(config)
    except (ValueError, OSError):
        return False
    return True


def wait_for_setup(config: Config):
    """Keep the agent alive without connecting or holding the authentication lock."""
    if ready_for_capture(config):
        return
    logging.info("Waiting for account setup. Run tgdaemon auth in your terminal.")
    while not ready_for_capture(config):
        time.sleep(5)
    logging.info("Account setup is ready; starting capture.")


def configure_logging(config: Config):
    log_dir = config.data_dir / "logs"
    private_directory(log_dir)
    handler = RotatingFileHandler(
        log_dir / "daemon.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().handlers = [handler]


def manage(action: str, config_path: Path, config: Config):
    if sys.platform != "darwin":
        raise ValueError("Built-in service management requires macOS; use systemd on Linux.")
    path = plist_path()
    domain = f"gui/{os.getuid()}"
    loaded = launchctl("print", target(), check=False)
    if action == "status":
        details = {}
        if loaded.returncode == 0:
            for key in ("state", "pid", "runs", "last exit code", "last terminating signal"):
                match = re.search(rf"^\s*{re.escape(key)} = (.+)$", loaded.stdout, re.MULTILINE)
                if match:
                    details[key] = match.group(1)
        print(
            json.dumps(
                {
                    "installed": path.is_file(),
                    "loaded": loaded.returncode == 0,
                    "launchd": details,
                    "account_setup_ready": ready_for_capture(config),
                    "config": str(config_path.resolve()),
                    "log": str(config.data_dir / "logs" / "daemon.log"),
                },
                indent=2,
            )
        )
        return
    if action == "install":
        private_directory(config.data_dir / "logs")
        if loaded.returncode == 0:
            launchctl("bootout", target())
        write_private(
            path, plistlib.dumps(build_plist(config_path, config, sys.executable)).decode()
        )
        launchctl("enable", target())
        launchctl("bootstrap", domain, str(path))
        print(f"Installed and started {LABEL}. Starts at login and restarts after exit.")
        if not ready_for_capture(config):
            print("Waiting for credentials and login. Run tgdaemon auth to finish setup.")
    elif action == "stop":
        launchctl("disable", target())
        if loaded.returncode == 0:
            launchctl("bootout", target())
        print("Service stopped and disabled until service start.")
    elif action in {"start", "restart"}:
        if not path.is_file():
            raise ValueError("Service is not installed; run tgdaemon service install.")
        if action == "restart" and loaded.returncode == 0:
            launchctl("bootout", target())
            loaded = None
        launchctl("enable", target())
        if loaded is None or loaded.returncode != 0:
            launchctl("bootstrap", domain, str(path))
        print("Service started.")
