from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import Config, private_directory
from .content import fingerprint, is_live_location_update

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    key TEXT PRIMARY KEY, namespace TEXT NOT NULL, message_id INTEGER NOT NULL,
    current_version INTEGER, deleted INTEGER NOT NULL DEFAULT 0,
    last_order INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL,
    UNIQUE(namespace, message_id)
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY, message_key TEXT NOT NULL REFERENCES messages(key) ON DELETE CASCADE,
    revision_key TEXT NOT NULL, fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL,
    media_state TEXT NOT NULL, media_path TEXT, UNIQUE(message_key, revision_key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, message_key TEXT NOT NULL REFERENCES messages(key) ON DELETE CASCADE,
    kind TEXT NOT NULL, before_id INTEGER REFERENCES versions(id),
    after_id INTEGER REFERENCES versions(id), observed REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
    retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    result_id INTEGER, error TEXT, UNIQUE(event_id, position)
);
CREATE INDEX IF NOT EXISTS messages_expires ON messages(expires);
CREATE INDEX IF NOT EXISTS versions_media ON versions(media_state);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text, content='');
"""


class Store:
    """One serialized connection. Mutating calls are atomic and safe to run via to_thread."""

    def __init__(self, path: Path, *, read_only=False):
        self.lock = threading.RLock()
        if read_only:
            if not path.exists():
                raise ValueError("No history database yet. Start the daemon first.")
            self.db = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=5000")
            return
        private_directory(path.parent)
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Database is from a newer application; upgrade before opening it")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA user_version=1")
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def bind_account(self, owner: int):
        saved = self.get("owner_id")
        if saved is not None and saved != owner:
            raise ValueError("This data directory belongs to another Telegram account")
        self.set("owner_id", owner)

    def recover(self):
        with self.lock, self.db:
            # A Bot API send has no idempotency key. Never blindly retry an interrupted send.
            self.db.execute(
                "UPDATE parts SET state='uncertain', error='interrupted_send' WHERE state='sending'"
            )
            self.db.execute(
                "UPDATE versions SET media_state='queued' WHERE media_state='downloading'"
            )

    def _version(self, version_id) -> dict | None:
        if not version_id:
            return None
        row = self.db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if not row:
            return None
        value = json.loads(row["snapshot"])
        value["version_id"] = row["id"]
        if value.get("media"):
            value["media"]["state"] = row["media_state"]
            value["media"]["path"] = row["media_path"]
        return value

    def version(self, version_id) -> dict | None:
        with self.lock:
            return self._version(version_id)

    def ingest(self, value: dict, edited: bool, config: Config, order: int = 0) -> int | None:
        key = f"{value['account_id']}:{value['namespace']}:{value['message_id']}"
        digest = fingerprint(value)
        revision = str(order) if order else f"{value.get('edit_at')}:{digest}"
        now = value["observed_at"]
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM messages WHERE key=?", (key,)).fetchone()
            if row and (row["deleted"] or (order and order < row["last_order"])):
                return None
            before = self._version(row["current_version"]) if row else None
            if before and fingerprint(before) == digest:
                self.db.execute(
                    "UPDATE messages SET last_order=MAX(last_order,?) WHERE key=?", (order, key)
                )
                return None
            # A stale edit with a known server timestamp must not overwrite a newer version.
            if before and (value.get("edit_at") or 0) < (before.get("edit_at") or 0):
                return None
            if not row:
                self.db.execute(
                    "INSERT INTO messages(key,namespace,message_id,expires) VALUES (?,?,?,?)",
                    (key, value["namespace"], value["message_id"], now + config.cache_days * 86400),
                )
            media = value.get("media") or {}
            state = media.get("state", "none")
            reused_path = None
            previous_media = (before or {}).get("media") or {}
            if (
                state == "metadata_only"
                and media.get("media_id")
                and previous_media.get("media_id") == media["media_id"]
                and previous_media.get("state") == "captured"
            ):
                state, reused_path = "captured", previous_media["path"]
            if config.download_media and media.get("media_id") and media["kind"] != "poll":
                if state == "metadata_only":
                    state = (
                        "queued"
                        if media.get("size", 0) <= config.max_file_mb * 1048576
                        else "skipped_size"
                    )
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO versions"
                "(message_key,revision_key,fingerprint,snapshot,media_state,media_path) "
                "VALUES (?,?,?,?,?,?)",
                (key, revision, digest, json.dumps(value), state, reused_path),
            )
            if inserted.rowcount == 0:
                return None
            version_id = inserted.lastrowid
            self.db.execute(
                "INSERT INTO search(rowid,text) VALUES (?,?)", (version_id, value["text"])
            )
            self.db.execute(
                "UPDATE messages SET current_version=?,last_order=MAX(last_order,?) WHERE key=?",
                (version_id, order, key),
            )
            if edited and not is_live_location_update(before, value):
                self.db.execute(
                    "UPDATE messages SET expires=? WHERE key=?",
                    (now + config.changed_days * 86400, key),
                )
                if config.monitor.notify_edits:
                    self.db.execute(
                        "INSERT INTO events(message_key,kind,before_id,after_id,observed) "
                        "VALUES (?,'edit',?,?,?)",
                        (key, before["version_id"] if before else None, version_id, now),
                    )
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('last_capture',?)", (json.dumps(now),)
            )
            return version_id

    def deletions(
        self,
        namespace: str,
        ids: list[int],
        config: Config,
        owner: int,
        bot_id: int,
        now: float | None = None,
    ) -> int:
        now = now or time.time()
        count = 0
        with self.lock, self.db:
            for message_id in ids:
                row = self.db.execute(
                    "SELECT * FROM messages WHERE namespace=? AND message_id=?",
                    (namespace, message_id),
                ).fetchone()
                if not row or row["deleted"]:
                    continue
                value = self._version(row["current_version"])
                if not config.monitor.accepts(value, owner, bot_id):
                    continue
                self.db.execute(
                    "UPDATE messages SET deleted=1,expires=? WHERE key=?",
                    (now + config.changed_days * 86400, row["key"]),
                )
                if config.monitor.notify_deletions:
                    self.db.execute(
                        "INSERT INTO events(message_key,kind,before_id,observed) "
                        "VALUES (?,'delete',?,?)",
                        (row["key"], row["current_version"], now),
                    )
                count += 1
        return count

    def pending_event(self) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM events WHERE state='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return {
                **dict(row),
                "before": self._version(row["before_id"]),
                "after": self._version(row["after_id"]),
            }

    def prepare(self, event_id: int, parts: list[dict]):
        with self.lock, self.db:
            for position, payload in enumerate(parts):
                self.db.execute(
                    "INSERT OR IGNORE INTO parts(event_id,position,payload) VALUES (?,?,?)",
                    (event_id, position, json.dumps(payload)),
                )
            self.db.execute("UPDATE events SET state='prepared' WHERE id=?", (event_id,))

    def cancel_event(self, event_id: int):
        with self.lock, self.db:
            self.db.execute("UPDATE events SET state='cancelled' WHERE id=?", (event_id,))
            self.db.execute(
                "UPDATE parts SET state='cancelled' WHERE event_id=? AND state!='sent'", (event_id,)
            )

    def next_part(self) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT p.* FROM parts p JOIN events e ON e.id=p.event_id "
                "WHERE p.state='pending' AND p.retry_at<=? AND e.state='prepared' "
                "AND NOT EXISTS (SELECT 1 FROM parts prev WHERE prev.event_id=p.event_id "
                "AND prev.position<p.position AND prev.state NOT IN ('sent','cancelled')) "
                "ORDER BY p.event_id,p.position LIMIT 1",
                (time.time(),),
            ).fetchone()
            return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def event_message(self, event_id: int):
        with self.lock:
            row = self.db.execute(
                "SELECT before_id,after_id FROM events WHERE id=?", (event_id,)
            ).fetchone()
            return self._version(row["after_id"] or row["before_id"]) if row else None

    def report_anchor(self, event_id: int):
        with self.lock:
            row = self.db.execute(
                "SELECT result_id FROM parts WHERE event_id=? AND position=0 AND state='sent'",
                (event_id,),
            ).fetchone()
            return row[0] if row else None

    def part_state(
        self, part_id: int, state: str, *, retry_at=0, error=None, result_id=None, payload=None
    ):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE parts SET state=?,retry_at=?,error=?,result_id=?,"
                "attempts=attempts+? WHERE id=?",
                (state, retry_at, error, result_id, int(state == "sending"), part_id),
            )
            if payload is not None:
                self.db.execute(
                    "UPDATE parts SET payload=? WHERE id=?", (json.dumps(payload), part_id)
                )

    def retry(self, uncertain=False):
        states = ("blocked", "failed", "uncertain") if uncertain else ("blocked", "failed")
        with self.lock, self.db:
            return self.db.execute(
                "UPDATE parts SET state='pending',retry_at=0 "
                f"WHERE state IN ({','.join('?' for _ in states)})",
                states,
            ).rowcount

    def media_job(self):
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM versions WHERE media_state='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            return self._version(row["id"]) if row else None

    def media_result(self, version_id: int, state: str, path: str | None = None):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE versions SET media_state=?,media_path=? WHERE id=?",
                (state, path, version_id),
            )

    def media_paths(self) -> set[str]:
        with self.lock:
            return {
                row[0]
                for row in self.db.execute(
                    "SELECT media_path FROM versions WHERE media_path IS NOT NULL"
                )
            }

    def stats(self) -> dict:
        with self.lock:
            states = dict(self.db.execute("SELECT state,COUNT(*) FROM parts GROUP BY state"))
            return {
                "messages": self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "versions": self.db.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
                "pending_events": self.db.execute(
                    "SELECT COUNT(*) FROM events WHERE state='pending'"
                ).fetchone()[0],
                "deliveries": states,
                "mode": self.get("mode"),
                "paused": self.get("paused", False),
                "last_capture": self.get("last_capture"),
                "heartbeat": self.get("heartbeat"),
                "unclean_starts": self.get("unclean_starts", 0),
                "last_disconnect": self.get("last_disconnect"),
                "bot_polling_conflict": self.get("bot_polling_conflict", False),
                "last_bot_polling_conflict": self.get("last_bot_polling_conflict"),
            }

    def search(self, query: str, limit=30) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT v.id FROM search JOIN versions v ON v.id=search.rowid "
                "WHERE search MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [self._version(row[0]) for row in rows]

    def history(self, version_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM versions WHERE message_key="
                "(SELECT message_key FROM versions WHERE id=?) ORDER BY id",
                (version_id,),
            ).fetchall()
            return [self._version(row[0]) for row in rows]

    def purge(self, now: float | None = None):
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT v.id,v.snapshot FROM versions v JOIN messages m "
                "ON m.key=v.message_key WHERE m.expires<?",
                (now or time.time(),),
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "INSERT INTO search(search,rowid,text) VALUES ('delete',?,?)",
                    (row["id"], json.loads(row["snapshot"])["text"]),
                )
            self.db.execute("DELETE FROM messages WHERE expires<?", (now or time.time(),))
            # Drop copies of report contents after confirmed delivery; retain only receipt metadata.
            self.db.execute("UPDATE parts SET payload='{}' WHERE state IN ('sent','cancelled')")
            return len(rows)

    def backup(self, destination: Path):
        with self.lock:
            with sqlite3.connect(destination) as other:
                self.db.backup(other)
            destination.chmod(0o600)
