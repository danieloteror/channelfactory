"""Worker persistente: reclama trabajos, mantiene heartbeat y clasifica errores.

Se ejecuta como proceso independiente (`channel-factory worker`), no depende de
una sesión de chat abierta. Tras una caída, los leases caducados se recuperan
en el siguiente `claim` y las etapas completadas no se repiten (caché por
`idempotency_key` y por `cache_key` de artefactos).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import ErrorClass
from .budget import BudgetBlocked
from .jobs import Job, JobQueue, LeaseLost

log = logging.getLogger("channel_factory.worker")


class StageError(RuntimeError):
    def __init__(self, message: str, error_class: ErrorClass = ErrorClass.transient):
        super().__init__(message)
        self.error_class = error_class


class Blocked(StageError):
    """Falta un acceso/modelo/decisión: bloqueo real, no éxito simulado."""

    def __init__(self, message: str):
        super().__init__(message, ErrorClass.blocked)


@dataclass
class StageContext:
    job: Job
    queue: JobQueue
    worker_id: str
    services: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    cancelled: threading.Event = field(default_factory=threading.Event)

    @property
    def deadline(self) -> float | None:
        return time.time() + self.job.timeout_s if self.job.timeout_s else None

    def add_cost(self, usd: float) -> None:
        self.cost_usd += usd


Handler = Callable[[StageContext], Any]
_HANDLERS: dict[str, Handler] = {}


def stage(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return deco


def handlers() -> dict[str, Handler]:
    return dict(_HANDLERS)


def run_one(queue: JobQueue, worker_id: str, services: dict | None = None, kinds: list[str] | None = None,
            heartbeat_s: float = 30.0) -> Job | None:
    job = queue.claim(worker_id, kinds=kinds or list(_HANDLERS))
    if job is None:
        return None
    ctx = StageContext(job=job, queue=queue, worker_id=worker_id, services=services or {})
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(heartbeat_s):
            try:
                queue.heartbeat(job.job_id, worker_id)
            except LeaseLost:
                ctx.cancelled.set()
                return
            except Exception:  # noqa: BLE001 - el heartbeat no debe matar el trabajo
                log.exception("heartbeat falló")

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        handler = _HANDLERS.get(job.kind)
        if handler is None:
            raise StageError(f"sin handler para {job.kind}", ErrorClass.permanent)
        result = handler(ctx)
        if ctx.cancelled.is_set():
            raise LeaseLost(job.job_id)
        queue.complete(job.job_id, worker_id, result, cost_usd=ctx.cost_usd)
    except LeaseLost:
        log.warning("lease perdido para %s; otro worker lo tiene", job.job_id)
    except BudgetBlocked as e:
        queue.fail(job.job_id, worker_id, str(e), ErrorClass.blocked)
    except StageError as e:
        queue.fail(job.job_id, worker_id, str(e), e.error_class)
    except Exception as e:  # noqa: BLE001
        queue.fail(job.job_id, worker_id, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}", ErrorClass.transient)
    finally:
        stop.set()
        t.join(timeout=1)
    return queue.get(job.job_id)


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def loop(queue: JobQueue, services: dict | None = None, idle_sleep: float = 5.0, once: bool = False,
         heartbeat_s: float = 30.0) -> None:
    worker_id = default_worker_id()
    stopping = threading.Event()

    def _sig(*_: Any) -> None:
        log.info("señal recibida: termino el trabajo actual y salgo")
        stopping.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except ValueError:  # no estamos en el hilo principal
            pass
    while not stopping.is_set():
        job = run_one(queue, worker_id, services, heartbeat_s=heartbeat_s)
        if once:
            return
        if job is None:
            stopping.wait(idle_sleep)
