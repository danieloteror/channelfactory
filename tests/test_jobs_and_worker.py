"""Riesgos 1, 3, 4 y 12: idempotencia, leases concurrentes, aislamiento de fallos y bloqueos reales."""

from __future__ import annotations

import threading
import time

from channel_factory.db import DB
from channel_factory.models import ErrorClass
from channel_factory.orchestrator.jobs import JobQueue, LeaseLost
from channel_factory.orchestrator.worker import Blocked, StageError, run_one, stage


def test_enqueue_is_idempotent(db: DB) -> None:
    q = JobQueue(db)
    a = q.enqueue("t.noop", {"x": 1}, "ep1:t.noop:v1")
    b = q.enqueue("t.noop", {"x": 1}, "ep1:t.noop:v1")
    assert a.job_id == b.job_id
    assert len(q.list()) == 1


def test_completed_stage_not_rerun_nor_recharged(db: DB) -> None:
    calls = []

    @stage("t.paid")
    def _paid(ctx):  # noqa: ANN001, ANN202
        calls.append(1)
        ctx.add_cost(0.5)
        return {"ok": True}

    q = JobQueue(db)
    q.enqueue("t.paid", {}, "ep:t.paid:v1")
    run_one(q, "w1", kinds=["t.paid"])
    # «reinicio»: se vuelve a encolar con la misma clave y otro worker procesa la cola
    again = q.enqueue("t.paid", {}, "ep:t.paid:v1")
    assert again.state == "succeeded"
    assert run_one(q, "w2", kinds=["t.paid"]) is None
    assert len(calls) == 1
    assert q.get(again.job_id).cost_usd == 0.5


def test_concurrent_claims_never_double(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "c.sqlite"
    seed = JobQueue(DB(path))
    for i in range(40):
        seed.enqueue("t.x", {}, f"k{i}")
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        q = JobQueue(DB(path))  # conexión propia, como otro proceso
        while (j := q.claim(f"w{n}", kinds=["t.x"])) is not None:
            with lock:
                claimed.append(j.job_id)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(claimed) == 40
    assert len(set(claimed)) == 40


def test_expired_lease_is_recovered_once(db: DB) -> None:
    q = JobQueue(db, lease_seconds=1)
    j = q.enqueue("t.y", {}, "k-lease")
    first = q.claim("dead-worker", now=time.time())
    assert first.job_id == j.job_id
    assert q.claim("w2", now=time.time()) is None  # lease vigente
    rec = q.claim("w2", now=time.time() + 5)  # lease caducado → recuperado
    assert rec.job_id == j.job_id and rec.attempts == 2
    try:
        q.complete(j.job_id, "dead-worker", {})  # el zombi no puede pisar al nuevo dueño
        raise AssertionError("debió fallar")
    except LeaseLost:
        pass
    q.complete(j.job_id, "w2", {"done": True})
    assert q.get(j.job_id).state == "succeeded"


def test_blocked_is_not_success(db: DB) -> None:
    @stage("t.needs_access")
    def _h(ctx):  # noqa: ANN001, ANN202
        raise Blocked("falta YOUTUBE_API_KEY")

    q = JobQueue(db)
    j = q.enqueue("t.needs_access", {}, "k-blocked")
    run_one(q, "w", kinds=["t.needs_access"])
    got = q.get(j.job_id)
    assert got.state == "blocked" and got.error_class == ErrorClass.blocked.value
    assert "YOUTUBE_API_KEY" in got.last_error


def test_failure_isolated_and_dependents_blocked(db: DB) -> None:
    @stage("t.scene")
    def _scene(ctx):  # noqa: ANN001, ANN202
        if ctx.job.payload["n"] == 2:
            raise StageError("escena 2 rota", ErrorClass.permanent)
        return {"scene": ctx.job.payload["n"]}

    @stage("t.after")
    def _after(ctx):  # noqa: ANN001, ANN202
        return {}

    q = JobQueue(db)
    scenes = [q.enqueue("t.scene", {"n": i}, f"s{i}") for i in range(1, 4)]
    dep = q.enqueue("t.after", {}, "after-2", depends_on=[scenes[1].job_id])
    for _ in range(5):
        run_one(q, "w", kinds=["t.scene", "t.after"])
    states = {s.job_id: q.get(s.job_id).state for s in scenes}
    assert states[scenes[0].job_id] == "succeeded" and states[scenes[2].job_id] == "succeeded"
    assert states[scenes[1].job_id] == "failed"
    q.claim("w", kinds=["t.after"])  # evaluar dependencias
    assert q.get(dep.job_id).state == "blocked"


def test_transient_retries_with_backoff(db: DB) -> None:
    q = JobQueue(db, backoff=[0, 0], max_attempts=2)
    j = q.enqueue("t.z", {}, "k-tr")
    c = q.claim("w")
    assert q.fail(c.job_id, "w", "timeout", ErrorClass.transient) == "queued"
    c = q.claim("w")
    assert q.fail(c.job_id, "w", "timeout", ErrorClass.transient) == "failed"
    assert q.get(j.job_id).attempts == 2
