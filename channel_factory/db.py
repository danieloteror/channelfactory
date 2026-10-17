"""Estado durable en SQLite (WAL) con migraciones versionadas.

Todas las escrituras que deciden propiedad (leases, locks, reservas de
presupuesto) se hacen dentro de `BEGIN IMMEDIATE`, que toma el bloqueo de
escritura de la base de datos: dos procesos no pueden reclamar el mismo
trabajo ni gastar la misma reserva a la vez.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS batches (
        batch_id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        month TEXT NOT NULL,
        status TEXT NOT NULL,
        plan_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(channel_id, month)
    );
    CREATE TABLE IF NOT EXISTS episodes (
        episode_id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        batch_id TEXT,
        slug TEXT NOT NULL,
        title TEXT,
        state TEXT NOT NULL,
        script_version INTEGER NOT NULL DEFAULT 1,
        slot_at TEXT,
        data_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(channel_id, slug)
    );
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        channel_id TEXT,
        episode_id TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        depends_on_json TEXT NOT NULL DEFAULT '[]',
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL,
        run_after REAL NOT NULL,
        lease_owner TEXT,
        lease_expires_at REAL,
        heartbeat_at REAL,
        timeout_s REAL,
        last_error TEXT,
        error_class TEXT,
        result_json TEXT,
        cost_usd REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(state, run_after);
    CREATE INDEX IF NOT EXISTS jobs_episode ON jobs(episode_id);
    CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY,
        episode_id TEXT,
        stage TEXT NOT NULL,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT,
        cache_key TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        inputs_json TEXT,
        valid INTEGER NOT NULL DEFAULT 1,
        retain_until TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(stage, cache_key)
    );
    CREATE TABLE IF NOT EXISTS budget_ledger (
        entry_id TEXT PRIMARY KEY,
        channel_id TEXT,
        batch_id TEXT,
        episode_id TEXT,
        month TEXT,
        provider TEXT NOT NULL,
        category TEXT NOT NULL,
        kind TEXT NOT NULL,
        amount_usd REAL NOT NULL,
        status TEXT NOT NULL,
        job_id TEXT,
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS publications (
        pub_id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        channel_id TEXT NOT NULL,
        episode_id TEXT,
        clip_id TEXT,
        network TEXT NOT NULL,
        post_type TEXT NOT NULL,
        brand_id TEXT,
        destination_id TEXT,
        media_path TEXT,
        media_url TEXT,
        media_sha256 TEXT,
        content_version INTEGER NOT NULL,
        scheduled_at TEXT,
        timezone TEXT,
        state TEXT NOT NULL,
        remote_id TEXT,
        planner_url TEXT,
        final_url TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_at TEXT,
        last_error TEXT,
        lock_owner TEXT,
        lock_expires_at REAL,
        request_json TEXT,
        response_json TEXT,
        content_json TEXT,
        content_fingerprint TEXT,
        transport TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS publications_channel ON publications(channel_id, state);
    CREATE TABLE IF NOT EXISTS ref_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_ref TEXT NOT NULL,
        video_id TEXT,
        as_of TEXT NOT NULL,
        views INTEGER,
        likes INTEGER,
        comments INTEGER,
        source TEXT NOT NULL,
        UNIQUE(channel_ref, video_id, as_of, source)
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        type TEXT NOT NULL,
        data_json TEXT
    );
    CREATE INDEX IF NOT EXISTS events_entity ON events(entity, entity_id);
    """,
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------ básico
    def _migrate(self) -> None:
        cur = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[cur:], start=cur + 1):
            # executescript() haría COMMIT implícito; ejecutamos sentencia a sentencia.
            with self.tx():
                for stmt in _split(sql):
                    self._conn.execute(stmt)
                self._conn.execute(f"PRAGMA user_version={i}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transacción con bloqueo de escritura inmediato (reintenta si está ocupada)."""
        for attempt in range(50):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:  # database is locked
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.05 * (attempt + 1))
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def event(self, entity: str, entity_id: str, type_: str, data: Any = None, conn: sqlite3.Connection | None = None) -> None:
        (conn or self._conn).execute(
            "INSERT INTO events(ts, entity, entity_id, type, data_json) VALUES (?,?,?,?,?)",
            (utcnow_iso(), entity, entity_id, type_, json.dumps(data, default=str) if data is not None else None),
        )

    def history(self, entity: str, entity_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM events WHERE entity=? AND entity_id=? ORDER BY id", (entity, entity_id))
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def open_db(settings) -> DB:  # noqa: ANN001 - Settings, evita import circular
    return DB(settings.db_path)
