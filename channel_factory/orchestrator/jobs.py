"""Cola persistente con leases, dependencias, reintentos y recuperación.

- `enqueue` es idempotente por `idempotency_key`: repetir un comando no crea
  trabajos duplicados ni vuelve a gastar en etapas completadas.
- `claim` reclama de forma atómica (BEGIN IMMEDIATE) un trabajo listo o uno
  cuyo lease caducó (worker caído). Nunca lo reclaman dos workers.
- `heartbeat`/`complete`/`fail` solo surten efecto si el worker sigue siendo
  el dueño del lease; un worker "zombi" no puede pisar a otro.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..db import DB, new_id, utcnow_iso
from ..models import ErrorClass, JobState


@dataclass
class Job:
    job_id: str
    kind: str
    channel_id: str | None
    episode_id: str | None
    idempotency_key: str
    payload: dict
    depends_on: list[str]
    state: str
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: float | None
    timeout_s: float | None
    last_error: str | None
    error_class: str | None
    result: Any
    cost_usd: float

    @classmethod
    def from_row(cls, r) -> "Job":  # noqa: ANN001
        return cls(
            job_id=r["job_id"],
            kind=r["kind"],
            channel_id=r["channel_id"],
            episode_id=r["episode_id"],
            idempotency_key=r["idempotency_key"],
            payload=json.loads(r["payload_json"]),
            depends_on=json.loads(r["depends_on_json"]),
            state=r["state"],
            attempts=r["attempts"],
            max_attempts=r["max_attempts"],
            lease_owner=r["lease_owner"],
            lease_expires_at=r["lease_expires_at"],
            timeout_s=r["timeout_s"],
            last_error=r["last_error"],
            error_class=r["error_class"],
            result=json.loads(r["result_json"]) if r["result_json"] else None,
            cost_usd=r["cost_usd"],
        )


class LeaseLost(RuntimeError):
    """El worker ya no es dueño del trabajo (lease caducado y reclamado)."""


class JobQueue:
    def __init__(self, db: DB, lease_seconds: int = 900, backoff: Iterable[int] = (30, 120, 600), max_attempts: int = 3):
        self.db = db
        self.lease_seconds = lease_seconds
        self.backoff = list(backoff)
        self.default_max_attempts = max_attempts

    # ------------------------------------------------------------ alta
    def enqueue(
        self,
        kind: str,
        payload: dict,
        idempotency_key: str,
        *,
        channel_id: str | None = None,
        episode_id: str | None = None,
        depends_on: list[str] | None = None,
        max_attempts: int | None = None,
        timeout_s: float | None = None,
        run_after: float | None = None,
    ) -> Job:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                return Job.from_row(row)
            job_id = new_id("job")
            now = utcnow_iso()
            c.execute(
                """INSERT INTO jobs(job_id, kind, channel_id, episode_id, idempotency_key, payload_json,
                       depends_on_json, state, attempts, max_attempts, run_after, timeout_s, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                (
                    job_id,
                    kind,
                    channel_id,
                    episode_id,
                    idempotency_key,
                    json.dumps(payload, sort_keys=True),
                    json.dumps(depends_on or []),
                    JobState.queued.value,
                    max_attempts or self.default_max_attempts,
                    run_after if run_after is not None else time.time(),
                    timeout_s,
                    now,
                    now,
                ),
            )
            self.db.event("job", job_id, "enqueued", {"kind": kind, "key": idempotency_key}, conn=c)
            return Job.from_row(c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def get(self, job_id: str) -> Job:
        row = self.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not row:
            raise KeyError(job_id)
        return Job.from_row(row)

    # ------------------------------------------------------------ reclamar
    def claim(self, worker_id: str, kinds: list[str] | None = None, now: float | None = None) -> Job | None:
        now = now if now is not None else time.time()
        with self.db.tx() as c:
            rows = c.execute(
                """SELECT * FROM jobs
                   WHERE (state='queued' AND run_after<=?)
                      OR (state='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<?)
                   ORDER BY run_after, created_at""",
                (now, now),
            ).fetchall()
            for r in rows:
                if kinds and r["kind"] not in kinds:
                    continue
                deps = json.loads(r["depends_on_json"])
                if deps:
                    states = {
                        x["job_id"]: x["state"]
                        for x in c.execute(
                            f"SELECT job_id, state FROM jobs WHERE job_id IN ({','.join('?' * len(deps))})", deps
                        ).fetchall()
                    }
                    if any(states.get(d) != JobState.succeeded.value for d in deps):
                        # Si una dependencia falló definitivamente, este trabajo queda bloqueado.
                        if any(states.get(d) in (JobState.failed.value, JobState.cancelled.value) for d in deps):
                            c.execute(
                                "UPDATE jobs SET state='blocked', error_class='blocked', last_error=?, updated_at=? WHERE job_id=?",
                                ("dependency failed", utcnow_iso(), r["job_id"]),
                            )
                        continue
                recovered = r["state"] == JobState.running.value
                attempts = r["attempts"] + 1
                if attempts > r["max_attempts"]:
                    c.execute(
                        "UPDATE jobs SET state='failed', error_class=?, last_error=?, lease_owner=NULL, updated_at=? WHERE job_id=?",
                        (ErrorClass.transient.value, "max attempts exceeded (lease expired)", utcnow_iso(), r["job_id"]),
                    )
                    continue
                c.execute(
                    """UPDATE jobs SET state='running', attempts=?, lease_owner=?, lease_expires_at=?,
                           heartbeat_at=?, updated_at=? WHERE job_id=?""",
                    (attempts, worker_id, now + self.lease_seconds, now, utcnow_iso(), r["job_id"]),
                )
                self.db.event(
                    "job", r["job_id"], "recovered" if recovered else "claimed", {"worker": worker_id, "attempt": attempts}, conn=c
                )
                return Job.from_row(c.execute("SELECT * FROM jobs WHERE job_id=?", (r["job_id"],)).fetchone())
        return None

    def _owned(self, c, job_id: str, worker_id: str) -> None:  # noqa: ANN001
        r = c.execute("SELECT lease_owner, state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not r or r["lease_owner"] != worker_id or r["state"] != JobState.running.value:
            raise LeaseLost(job_id)

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        now = time.time()
        with self.db.tx() as c:
            self._owned(c, job_id, worker_id)
            c.execute(
                "UPDATE jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=? WHERE job_id=?",
                (now, now + self.lease_seconds, utcnow_iso(), job_id),
            )

    def complete(self, job_id: str, worker_id: str, result: Any = None, cost_usd: float = 0.0) -> None:
        with self.db.tx() as c:
            self._owned(c, job_id, worker_id)
            c.execute(
                """UPDATE jobs SET state='succeeded', result_json=?, cost_usd=cost_usd+?, lease_owner=NULL,
                       lease_expires_at=NULL, last_error=NULL, error_class=NULL, updated_at=? WHERE job_id=?""",
                (json.dumps(result, default=str), cost_usd, utcnow_iso(), job_id),
            )
            self.db.event("job", job_id, "succeeded", {"cost_usd": cost_usd}, conn=c)

    def fail(self, job_id: str, worker_id: str, error: str, error_class: ErrorClass = ErrorClass.transient) -> str:
        with self.db.tx() as c:
            self._owned(c, job_id, worker_id)
            r = c.execute("SELECT attempts, max_attempts FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if error_class == ErrorClass.transient and r["attempts"] < r["max_attempts"]:
                delay = self.backoff[min(r["attempts"] - 1, len(self.backoff) - 1)] if self.backoff else 0
                state = JobState.queued.value
                run_after = time.time() + delay
            else:
                state = JobState.blocked.value if error_class == ErrorClass.blocked else JobState.failed.value
                run_after = time.time()
            c.execute(
                """UPDATE jobs SET state=?, last_error=?, error_class=?, run_after=?, lease_owner=NULL,
                       lease_expires_at=NULL, updated_at=? WHERE job_id=?""",
                (state, error[:4000], error_class.value, run_after, utcnow_iso(), job_id),
            )
            self.db.event("job", job_id, "failed", {"class": error_class.value, "error": error[:500], "next": state}, conn=c)
            return state

    def cancel(self, job_id: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE jobs SET state='cancelled', lease_owner=NULL, updated_at=? WHERE job_id=? AND state IN ('queued','blocked','running','failed')",
                (utcnow_iso(), job_id),
            )
            self.db.event("job", job_id, "cancelled", None, conn=c)

    def retry(self, job_id: str) -> None:
        """Reencola un trabajo fallido/bloqueado (orden manual: no cuenta como intento extra)."""
        with self.db.tx() as c:
            c.execute(
                """UPDATE jobs SET state='queued', attempts=0, run_after=?, last_error=NULL, error_class=NULL,
                       updated_at=? WHERE job_id=? AND state IN ('failed','blocked','cancelled')""",
                (time.time(), utcnow_iso(), job_id),
            )
            self.db.event("job", job_id, "retry_requested", None, conn=c)

    def list(self, *, channel_id: str | None = None, episode_id: str | None = None, state: str | None = None) -> list[Job]:
        sql, params = "SELECT * FROM jobs WHERE 1=1", []
        if channel_id:
            sql += " AND channel_id=?"
            params.append(channel_id)
        if episode_id:
            sql += " AND episode_id=?"
            params.append(episode_id)
        if state:
            sql += " AND state=?"
            params.append(state)
        return [Job.from_row(r) for r in self.db.query(sql + " ORDER BY created_at", tuple(params))]
