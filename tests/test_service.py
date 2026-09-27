import json
import plistlib
from pathlib import Path

from telegram_daemon.service import build_plist, ready_for_capture


def test_launch_agent_survives_login_and_restarts_without_a_shell_or_secrets(config):
    python = "/Users/example/Library/Application Support/tools/bin/python"
    path = config.data_dir / "config with spaces.toml"
    data = build_plist(path, config, python)
    decoded = plistlib.loads(plistlib.dumps(data))
    assert decoded["ProgramArguments"] == [
        python,
        "-m",
        "telegram_daemon.cli",
        "--config",
        str(path),
        "service-run",
    ]
    assert decoded["RunAtLoad"] and decoded["KeepAlive"]
    assert decoded["ThrottleInterval"] >= 10
    assert decoded["Umask"] == 0o077
    assert decoded["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1"}


def test_service_waits_for_completed_authentication(config, monkeypatch):
    for name in ("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert not ready_for_capture(config)
    (config.data_dir / "credentials.json").write_text(
        json.dumps(
            {"api_id": 123, "api_hash": "synthetic-hash", "bot_token": "123:synthetic-token"}
        )
    )
    (config.data_dir / "user.session").touch()
    assert not ready_for_capture(config)
    (config.data_dir / "auth.ready").write_text("123")
    assert ready_for_capture(config)


def test_launch_agent_keeps_virtualenv_symlink_path(config, tmp_path):
    executable = tmp_path / "env/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to("/usr/bin/python3")
    data = build_plist(Path("config.toml"), config, str(executable))
    assert data["ProgramArguments"][0] == str(executable)
