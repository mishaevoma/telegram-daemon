# telegram-daemon

A personal Telegram daemon and private notification bot. It captures messages through your logged-in account and sends readable reports when captured messages are edited or deleted.

**Personal-only mode is the default:** human one-to-one conversations, in both directions. Groups, channels, bots, Telegram service messages, and Saved Messages are excluded. Change scope while running with `/mode personal`, `/mode selected`, or `/mode all` in your bot.

One Python process uses Telethon, the Telegram Bot API, and SQLite. Linux and macOS are supported. No public HTTP server or AI service is required.

**Reports**

Edit reports show the chat, author, timestamps, and **Before / After** content. Deletion reports include the last captured version. Supported formatting is preserved through Telegram entities, including Unicode/emoji offsets, bold, italic, underline, strikethrough, links, spoilers, quotes, and code blocks. Message content is never interpreted as Markdown or HTML.

Long reports are split within Telegram's limits. Continuations and attachments reply to the first report message. If Telegram rejects formatting, that part retries as plain text. Custom emoji use their underlying Unicode text. Deletion reports identify the message author, not who deleted it; timestamps describe when the daemon observed the deletion.

| Telegram content | Handling |
| --- | --- |
| Text and captions | Observed versions with supported formatting |
| Photos, documents, video, audio, voice messages | Bounded local capture, then bot upload |
| Stickers, animations, round videos | Attribute-based classification; native send with document fallback |
| Contacts, locations, venues, polls, dice | Readable summaries |
| Albums | Individual messages with album context |
| Expiring or protected attachments | Described without downloading |
| Other/new objects | Readable fallback; no executable Python deserialization |

Poll vote counts and link-preview refreshes do not create edit alerts. Reports explain attachments that are too large, unavailable, still downloading, or outside the storage budget. Downloads completed after the report's wait window remain local; they are not automatically sent later.

Live-location movement, heading, accuracy, and broadcast-period updates are saved in history without creating edit alerts. A live location first encountered mid-broadcast establishes a quiet baseline when it has no text. Text/formatting changes, media replacements, and deletions still generate reports; a deletion includes the latest captured coordinates. Routine location updates use the ordinary cache retention and do not extend it to the changed-message retention period.

**Setup**

Use Python 3.11+ and [uv](https://docs.astral.sh/uv/). From the checkout:

```sh
uv sync --frozen
uv run tgdaemon init
uv run tgdaemon auth
uv run tgdaemon run
```

You need your Telegram API ID/hash from [my.telegram.org](https://my.telegram.org) and a token for a bot you own. Create the Telegram bot identity by sending `/newbot` to [@BotFather](https://t.me/BotFather). [Official instructions](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).

`auth` prompts for credentials, your phone number, the login code, and your 2FA password if required. Hash/token prompts are hidden. API credentials and the token go into a restricted local `credentials.json`; login codes and 2FA passwords are not retained. Alternatively inject `TG_API_ID`, `TG_API_HASH`, and `TG_BOT_TOKEN` through your service's environment.

Start the daemon, then open your bot and send `/start`. Reports stay queued until then. Only the numeric account authenticated by the daemon can receive reports or control it. Other users and group commands are ignored. Use a dedicated bot without an existing webhook or another polling process.

If another process polls the same bot or a webhook conflicts, command polling backs off for 60 seconds while capture and report delivery continue. `tgdaemon status` exposes the polling conflict and its last occurrence. Stop the competing poller or use a dedicated bot to restore reliable commands. Reports still require prior `/start` activation.

`init` prints the config path. Data lives outside the checkout in the OS application-data directory; `tgdaemon doctor` shows resolved paths. Every command accepts a leading `--config /absolute/path/config.toml`. See the [configuration template](src/telegram_daemon/example.toml).

**Bot controls**

| Command | Action |
| --- | --- |
| `/start`, `/help` | Setup guidance and command list |
| `/mode personal` | Human direct conversations only |
| `/mode selected` | Only numeric `monitor.chat_ids` configured locally |
| `/mode all` | All accessible chat types, with configured exclusions |
| `/status` | Connection, mode, retained counts, pending/failed/uncertain deliveries |
| `/pause`, `/resume` | Pause/resume capture and reports |
| `/retry` | Retry known failed or blocked deliveries |
| `/retry uncertain` | Explicitly retry unknown outcomes; duplicates are possible |

Mode changes persist across restarts and override `monitor.mode` in the file. They filter queued reports too. Previously captured history remains until expiry. While stopped, `tgdaemon mode personal` sets the same preference. Other config changes require a restart.

Use `tgdaemon sources` while stopped to list signed Telegram peer IDs. An empty selected list captures nothing. Set `include_outgoing = false` to omit messages you send. Personal mode always excludes bots; `include_bots` only affects selected/all modes. Pausing creates a potential coverage gap.

**Storage and reliability**

Defaults: seven-day ordinary cache, 90 days for observed changed/deleted messages, 20 MiB per attachment, 2 GiB attachment budget, and a 256 MiB free-space reserve. Set `media.download = false` for metadata only. Retention changes affect future records; existing records keep their assigned expiry.

Versions and notification intents commit together in one transaction. Channel and personal-message IDs stay in separate namespaces. Replayed updates do not duplicate local versions or reports; edit reversions remain visible. Persistence failures stop capture rather than silently discard updates. Hourly maintenance expires content, search entries, notification payloads, and unreferenced media.

The Bot API has no client idempotency key for sending. Interrupted/unconfirmed sends become **uncertain**, requiring `/retry uncertain` instead of blind retries. Confirmed rate-limit rejections and connection failures before sending are retried. If you block the bot, unblock it and send `/start` to resume delivery.

Telegram may omit deletion updates. Messages deleted before capture cannot be reconstructed. Catch-up is requested at startup; session update cursors and application commits are separate, so crashes/offline intervals can leave gaps. This baseline captures future updates and supported catch-up, without historical backfill. [Telethon's deletion-event limitations](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.messagedeleted.MessageDeleted).

**Local tools**

```sh
uv run tgdaemon status
uv run tgdaemon doctor
uv run tgdaemon search 'deadline'
uv run tgdaemon history 12
uv run tgdaemon export 12 > history.json
```

Search uses SQLite FTS5 syntax and includes retained old versions. History/export uses a record number from a report/search; export writes JSON. These commands use read-only database connections and work offline. `doctor` checks local settings/capabilities, not live credential validity.

Stop the daemon for backup/restore; commands enforce the instance lock:

```sh
uv run tgdaemon backup /absolute/path/new-backup
uv run tgdaemon --config /absolute/path/restore-config.toml restore /absolute/path/new-backup
```

Backups include a SQLite snapshot and hashed attachment manifest, excluding credentials/session. Restore verifies integrity and requires no existing destination history database. Restored capture/reports start paused, and old unfinished sends require explicit handling. Authenticate the same account, start, then use `/start` and `/resume`.

Data is plain SQLite/files protected by filesystem permissions, not application-level encryption. Use an encrypted host volume if needed. Local expiry does not erase delivered Telegram reports, exports, or backups, or guarantee forensic disk erasure. Keep private state outside the public repository; ignore rules also cover accidental copies. Logs omit message bodies and token-bearing request URLs.

**Continuous operation**

On macOS, install the tool and its LaunchAgent:

```sh
uv tool install .
tgdaemon init                  # Skip if config already exists
tgdaemon service install
tgdaemon auth
tgdaemon service status
```

The agent starts at login and restarts after exit, with a 30-second restart throttle. Before account setup, it stays idle without opening Telegram or locking authentication. Successful `auth` writes a local readiness marker; the agent begins capture automatically. Open the bot and send `/start` to enable reports.

Use `tgdaemon service stop`, `start`, or `restart` to control it. `stop` also disables automatic launch until `start`. Stop it before re-authentication, source listing, backup, or package upgrades; use `service install` again after reinstalling the executable if its path changed. Existing sessions created by an earlier version need one `tgdaemon auth` to create the readiness marker.

`service status` reports the launchd PID, setup readiness, config path, and log location. Application logs rotate at 5 MiB with three backups under the data directory's `logs/`. The plist contains paths and restart settings, never credentials. It runs as your logged-in user; it cannot capture while the Mac sleeps or you are logged out. [Apple's LaunchAgent lifecycle](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

Keep the process on an awake machine. For a Linux user service, install with `uv tool install .`, copy [the systemd unit](deploy/telegram-daemon.service) to `~/.config/systemd/user/`, verify its executable path, and run `systemctl --user enable --now telegram-daemon`. Authenticate interactively as the same user first. User services require an active session or separately configured lingering.

Docker Compose uses persistent config/data volumes and an unprivileged user:

```sh
docker compose build
docker compose run --rm daemon init
docker compose run --rm daemon auth
docker compose up -d
docker compose logs -f
```

`docker compose down` keeps history unless `--volumes` is added. Foreground operation is supported on macOS; sleep interrupts capture.

**Development**

```sh
uv sync --frozen
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
uv build
```

Tests use synthetic Telegram objects and mocked network transport. Before trusting a deployment, run a live check in a dedicated conversation: send/edit/delete formatted text and an attachment, restart, and inspect `/status`.

An optional [GitHub Actions template](deploy/github-actions-checks.yml) runs checks on Python 3.11–3.13. To enable it, copy it to `.github/workflows/checks.yml` and publish using credentials with workflow permission. It remains inactive in the template location.

The broader [design](DESIGN.md) includes future watches, digests, and collections. Those, edit debouncing, quiet hours, automatic backfill, and complete album reconstruction are not implemented in this baseline. Inspired by [tg-notify-deleted-messages](https://github.com/mishaevoma/tg-notify-deleted-messages).
