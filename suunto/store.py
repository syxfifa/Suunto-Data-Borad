"""SQLite storage for observed MCP records and synchronization state."""

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3


SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS records (
    source TEXT NOT NULL,
    record_key TEXT NOT NULL,
    occurred_at TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    PRIMARY KEY (source, record_key)
);
CREATE INDEX IF NOT EXISTS records_time ON records(source, occurred_at);
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT PRIMARY KEY,
    scanned_until TEXT NOT NULL,
    full_scan_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    changed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sync_errors (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    source TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "suunto.sqlite"

    @contextmanager
    def connect(self, *, check_same_thread=True):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=check_same_thread)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("UPDATE sync_runs SET status='interrupted' WHERE status='running'")

    def query(self, statement, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(statement, params)]

    def cursor(self, source):
        seen = self.query("SELECT scanned_until, full_scan_at FROM cursors WHERE source=?", (source,))
        return seen[0] if seen else None

    def start(self, trigger, at):
        with self.connect() as db:
            return db.execute("INSERT INTO sync_runs(trigger, started_at, status) VALUES (?, ?, 'running')",
                              (trigger, at)).lastrowid

    def finish(self, run_id, at, status):
        with self.connect() as db:
            db.execute("UPDATE sync_runs SET status=?, finished_at=? WHERE id=?", (status, at, run_id))

    def error(self, run_id, source, message):
        with self.connect() as db:
            db.execute("INSERT INTO sync_errors(run_id, source, message) VALUES (?, ?, ?)",
                       (run_id, source, message))

    def commit(self, run_id, source, rows, *, until=None, full_scan_at=None):
        """Commit the records and cursor as one operation, including identical replay."""
        with self.connect() as db:
            saved = db.executemany("""
                INSERT INTO records(source, record_key, occurred_at, payload) VALUES (?, ?, ?, ?)
                ON CONFLICT(source, record_key) DO UPDATE SET
                  occurred_at=excluded.occurred_at, payload=excluded.payload
                WHERE records.payload <> excluded.payload OR records.occurred_at IS NOT excluded.occurred_at
                """, ((source, key, at, encode(payload)) for key, at, payload in rows)).rowcount
            db.execute("UPDATE sync_runs SET received=received+?, changed=changed+? WHERE id=?",
                       (len(rows), saved, run_id))
            if until is not None:
                db.execute("""
                    INSERT INTO cursors(source, scanned_until, full_scan_at) VALUES (?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                      scanned_until=max(cursors.scanned_until, excluded.scanned_until),
                      full_scan_at=coalesce(excluded.full_scan_at, cursors.full_scan_at)
                    """, (source, until, full_scan_at))

    def record(self, source, key):
        found = self.query("SELECT payload FROM records WHERE source=? AND record_key=?", (source, key))
        return json.loads(found[0]["payload"]) if found else None

    def status(self):
        success = self.query("SELECT max(finished_at) AS at FROM sync_runs WHERE status='ok'")[0]["at"]
        return {
            "last_success_at": success,
            "counts": {row["source"]: row["n"] for row in self.query(
                "SELECT source, count(*) AS n FROM records GROUP BY source")},
            "runs": self.query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 20"),
            "errors": self.query("SELECT * FROM sync_errors ORDER BY id DESC LIMIT 50"),
        }
