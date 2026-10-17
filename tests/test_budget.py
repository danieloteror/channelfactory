"""Riesgo 6: el lote no supera los topes ni pierde reservas en concurrencia."""

from __future__ import annotations

import threading

import pytest

from channel_factory.db import DB
from channel_factory.orchestrator.budget import Budget, BudgetBlocked, Caps


def test_paid_disabled_blocks(db: DB) -> None:
    b = Budget(db, Caps(False, 5, 50, 100))
    assert b.reserve(0, provider="kokoro", category="tts", episode_id="e") is None  # gratuito: sin reserva
    with pytest.raises(BudgetBlocked):
        b.reserve(0.1, provider="flux", category="image", episode_id="e")


def test_no_cap_means_no_spend(db: DB) -> None:
    b = Budget(db, Caps(True, None, None, None))
    with pytest.raises(BudgetBlocked):
        b.reserve(1.0, provider="flux", category="image", episode_id="e")


def test_concurrent_reservations_respect_cap(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "b.sqlite"
    DB(path)
    ok, blocked = [], []
    lock = threading.Lock()

    def worker() -> None:
        b = Budget(DB(path), Caps(True, None, 10.0, None))
        for _ in range(10):
            try:
                rid = b.reserve(0.75, provider="api", category="image", batch_id="B1")
                with lock:
                    ok.append(rid)
            except BudgetBlocked:
                with lock:
                    blocked.append(1)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(ok) == 13  # floor(10 / 0.75)
    b = Budget(DB(path), Caps(True, None, 10.0, None))
    for i, rid in enumerate(ok):
        if i % 3 == 0:
            b.release(rid)
        else:
            b.commit(rid, 0.5)
    rep = b.report(batch_id="B1")["image"]
    assert rep["released"] == pytest.approx(0.75 * 5)
    assert rep["charged"] == pytest.approx(0.5 * 8)
    assert "reserved" not in rep  # ninguna reserva perdida
