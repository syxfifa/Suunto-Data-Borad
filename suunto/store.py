"""SQLite storage for observed MCP records and synchronization state."""

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
from uuid import uuid4


SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    source TEXT NOT NULL,
    record_key TEXT NOT NULL,
    occurred_at TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    PRIMARY KEY (source, record_key)
);
CREATE INDEX IF NOT EXISTS records_time ON records(source, occurred_at);
CREATE INDEX IF NOT EXISTS workouts_sport_time ON records(json_extract(payload, '$.activityName'), occurred_at)
    WHERE source='workouts';
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
CREATE INDEX IF NOT EXISTS sync_errors_run ON sync_errors(run_id);
CREATE INDEX IF NOT EXISTS sync_runs_finished ON sync_runs(coalesce(finished_at, started_at));
CREATE TABLE IF NOT EXISTS coverage (
    source TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    PRIMARY KEY (source, start_at)
);
CREATE TABLE IF NOT EXISTS backfill (
    source TEXT PRIMARY KEY,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    next_at TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS sync_failures (
    source TEXT PRIMARY KEY,
    start_at TEXT,
    end_at TEXT,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_windows (
    source TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    message TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    retry_at TEXT NOT NULL,
    PRIMARY KEY (source, start_at, end_at)
);
CREATE INDEX IF NOT EXISTS sync_windows_due ON sync_windows(retry_at);
CREATE TABLE IF NOT EXISTS sync_queue (
    kind TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    revision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workout_details (
    record_key TEXT PRIMARY KEY,
    fingerprint TEXT,
    status TEXT NOT NULL,
    retry_at TEXT
);
CREATE TABLE IF NOT EXISTS source_success (
    source TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS removed_routes (
    record_key TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS route_absences (
    record_key TEXT PRIMARY KEY
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
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= version <= SCHEMA_VERSION:
                raise ValueError(f"SQLite 结构版本 {version} 不受本版本支持。")
            db.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                # Version 0 includes both fresh databases and existing unversioned releases.
                db.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA user_version=1;")
            db.execute("""
                INSERT OR IGNORE INTO sync_windows
                SELECT source, start_at, end_at, message, 1, end_at
                FROM sync_failures WHERE start_at IS NOT NULL AND end_at IS NOT NULL
                """)
            db.execute("DELETE FROM sync_failures WHERE start_at IS NOT NULL AND end_at IS NOT NULL")
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

    def prune_logs(self, before):
        with self.connect() as db:
            old_runs = """
                SELECT id FROM sync_runs WHERE coalesce(finished_at, started_at) < ?
                AND status!='running' AND id IS NOT coalesce((
                    SELECT id FROM sync_runs WHERE status='ok'
                    AND trigger NOT IN ('backfill', 'details', 'route_details')
                    ORDER BY finished_at DESC LIMIT 1), -1)
            """
            db.execute(f"DELETE FROM sync_errors WHERE run_id IN ({old_runs})", (before,))
            db.execute(f"DELETE FROM sync_runs WHERE id IN ({old_runs})", (before,))

    def finish(self, run_id, at, status):
        with self.connect() as db:
            db.execute("UPDATE sync_runs SET status=?, finished_at=? WHERE id=?", (status, at, run_id))

    def error(self, run_id, source, message):
        with self.connect() as db:
            db.execute("INSERT INTO sync_errors(run_id, source, message) VALUES (?, ?, ?)",
                       (run_id, source, message))

    def commit(self, run_id, source, rows, *, until=None, full_scan_at=None,
               interval=None, history_until=None, active_routes=None, details=(), checked_at=None):
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
            if interval is not None:
                start, end = interval
                db.execute("DELETE FROM sync_windows WHERE source=? AND start_at>=? AND end_at<=?",
                           (source, start, end))
                previous = db.execute("""
                    SELECT min(start_at), max(end_at) FROM coverage
                    WHERE source=? AND start_at<=? AND end_at>=?
                    """, (source, end, start)).fetchone()
                db.execute("DELETE FROM coverage WHERE source=? AND start_at<=? AND end_at>=?",
                           (source, end, start))
                db.execute("INSERT INTO coverage VALUES (?, ?, ?)",
                           (source, min(start, previous[0] or start), max(end, previous[1] or end)))
            if history_until is not None:
                db.execute("UPDATE backfill SET next_at=?, error=NULL WHERE source=?", (history_until, source))
            if checked_at is not None:
                db.execute("INSERT OR REPLACE INTO source_success VALUES (?, ?)", (source, checked_at))
            db.executemany("INSERT OR REPLACE INTO workout_details VALUES (?, ?, ?, ?)", details)
            if source == "routes":
                db.executemany("DELETE FROM removed_routes WHERE record_key=?", ((key,) for key, _, _ in rows))
                db.executemany("DELETE FROM route_absences WHERE record_key=?", ((key,) for key, _, _ in rows))
            if active_routes is not None:
                db.execute("""
                    INSERT OR IGNORE INTO removed_routes
                    SELECT record_key FROM route_absences
                    """)
                db.executemany("DELETE FROM removed_routes WHERE record_key=?", ((key,) for key in active_routes))
                db.execute("""
                    INSERT OR IGNORE INTO route_absences
                    SELECT record_key FROM records WHERE source='routes'
                    """)
                db.executemany("DELETE FROM route_absences WHERE record_key=?", ((key,) for key in active_routes))

    def coverage(self, source):
        return self.query("SELECT start_at, end_at FROM coverage WHERE source=? ORDER BY start_at", (source,))

    def failure(self, source, start, end, message):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO sync_failures VALUES (?, ?, ?, ?)", (source, start, end, message))

    def clear_failure(self, source):
        with self.connect() as db:
            db.execute("DELETE FROM sync_failures WHERE source=?", (source,))

    def windows_due(self, at, sources, limit):
        placeholders = ",".join("?" for _ in sources)
        return self.query(f"""
            SELECT * FROM sync_windows WHERE retry_at<=? AND source IN ({placeholders})
            ORDER BY retry_at, source, start_at LIMIT ?
            """, (at, *sources, limit))

    def clear_windows(self, source, start, end):
        with self.connect() as db:
            db.execute("DELETE FROM sync_windows WHERE source=? AND start_at>=? AND end_at<=?", (source, start, end))

    def window_failure(self, source, start, end, message, at, *, retry=True):
        with self.connect() as db:
            row = db.execute("SELECT attempts FROM sync_windows WHERE source=? AND start_at=? AND end_at=?",
                             (source, start, end)).fetchone()
            if row and not retry:
                return
            attempts = row[0] + 1 if row else 1
            retry_at = at + timedelta(minutes=15 * 2 ** min(attempts - 1, 6))
            db.execute("INSERT OR REPLACE INTO sync_windows VALUES (?, ?, ?, ?, ?, ?)",
                       (source, start, end, message, attempts, retry_at.isoformat()))

    @staticmethod
    def _enqueue(db, kind, trigger):
        db.execute("""
            INSERT INTO sync_queue VALUES (?, ?, ?)
            ON CONFLICT(kind) DO UPDATE SET trigger=excluded.trigger, revision=excluded.revision
            WHERE excluded.trigger<>'scheduled'
            """, (kind, trigger, uuid4().hex))

    def enqueue(self, kind, trigger):
        with self.connect() as db:
            self._enqueue(db, kind, trigger)

    def queued(self):
        return {row["kind"]: row for row in self.query("SELECT * FROM sync_queue")}

    def dequeue(self, kind, revision=None):
        with self.connect() as db:
            db.execute("DELETE FROM sync_queue WHERE kind=? AND (? IS NULL OR revision=?)",
                       (kind, revision, revision))

    def prepare_backfill(self, sources, start, end, *, activate=False):
        with self.connect() as db:
            db.execute("DELETE FROM backfill")
            db.executemany("INSERT INTO backfill VALUES (?, ?, ?, ?, NULL)",
                           ((source, start, end, start) for source in sources))
            if activate:
                self._enqueue(db, "backfill", "backfill")

    def backfill_error(self, source, message):
        with self.connect() as db:
            db.execute("UPDATE backfill SET error=? WHERE source=?", (message, source))

    def detail_state(self, key, fingerprint, status, retry_at=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO workout_details VALUES (?, ?, ?, ?)",
                       (key, fingerprint, status, retry_at))

    def detail_states(self, keys):
        keys = tuple(keys)
        placeholders = ",".join("?" for _ in keys)
        return {row["record_key"]: row for row in self.query(
            f"SELECT * FROM workout_details WHERE record_key IN ({placeholders})", keys)} if keys else {}

    def details_due(self, at, limit):
        return self.query("""
            SELECT r.record_key, r.occurred_at, r.payload, d.fingerprint
            FROM records r LEFT JOIN workout_details d USING(record_key)
            WHERE r.source='workouts' AND
                  (d.status='pending' AND (d.retry_at IS NULL OR d.retry_at<=?)
                   OR d.status IS NULL AND json_type(r.payload, '$.summary') IS NOT 'object')
            ORDER BY coalesce(d.retry_at, ''), r.occurred_at DESC LIMIT ?
            """, (at, limit))

    def record(self, source, key):
        found = self.query("SELECT payload FROM records WHERE source=? AND record_key=?", (source, key))
        return json.loads(found[0]["payload"]) if found else None

    def observed_entry_fields(self, source, since):
        return self.query("""
            SELECT field.key AS name, field.type AS type, count(*) AS samples,
                   sum(CASE WHEN field.type IN ('integer', 'real') AND field.value>=0 THEN 1 ELSE 0 END) AS nonnegative,
                   sum(CASE WHEN field.type='text' AND field.value GLOB '????-??-??*'
                            AND datetime(field.value) IS NOT NULL THEN 1 ELSE 0 END) AS timestamps
            FROM records
            JOIN json_each(records.payload, '$.entryData') AS field
            WHERE records.source=? AND records.occurred_at>=? AND field.type IN ('text', 'integer', 'real')
            GROUP BY field.key, field.type ORDER BY name
            """, (source, since))

    def status(self):
        success = self.query("""
            SELECT max(finished_at) AS at FROM sync_runs
            WHERE status='ok' AND trigger NOT IN ('backfill', 'details', 'route_details')
            """)[0]["at"]
        windows = self.query("SELECT * FROM sync_windows ORDER BY source, start_at, end_at")
        source_failures = {row["source"]: row for row in windows}
        source_failures.update((row["source"], row) for row in self.query("SELECT * FROM sync_failures"))
        return {
            "last_success_at": success,
            "counts": {row["source"]: row["n"] for row in self.query(
                "SELECT source, count(*) AS n FROM records GROUP BY source")},
            "runs": self.query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 20"),
            "errors": self.query("SELECT * FROM sync_errors ORDER BY id DESC LIMIT 50"),
            "backfill": self.query("SELECT * FROM backfill ORDER BY rowid"),
            "window_failures": windows,
            "failures": source_failures,
            "source_success": {r["source"]: r["checked_at"] for r in self.query("SELECT * FROM source_success")},
            "sources": self.query("""
                SELECT source, count(*) AS n, min(occurred_at) AS first, max(occurred_at) AS last
                FROM records WHERE source!='routes' OR record_key NOT IN (SELECT record_key FROM removed_routes)
                GROUP BY source
                """),
        }
