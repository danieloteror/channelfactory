"""Outbox/ledger de publicaciones con deduplicación, locks y conciliación.

- Clave de deduplicación por pieza/destino/versión: repetir `prepare` no duplica.
- Lock por publicación (BEGIN IMMEDIATE): dos workers no envían la misma pieza.
- Un timeout o respuesta ambigua tras enviar → `unconfirmed`: el siguiente intento
  DEBE conciliar con el estado remoto antes de reenviar.
- `scheduled` ≠ `published`: publicado solo con confirmación real (URL final).
No se promete exactly-once: el proveedor no lo garantiza; se minimiza el riesgo.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from ..db import DB, new_id, utcnow_iso
from ..models import PublicationState as S

TRANSITIONS: dict[str, set[str]] = {
    S.draft: {S.validated, S.blocked, S.cancelled, S.failed},
    S.validated: {S.ready, S.blocked, S.cancelled, S.draft},
    S.ready: {S.submitting, S.blocked, S.cancelled, S.draft},
    S.submitting: {S.scheduled, S.unconfirmed, S.failed, S.blocked, S.handoff, S.ready},
    S.handoff: {S.scheduled, S.failed, S.unconfirmed, S.cancelled},
    S.unconfirmed: {S.scheduled, S.ready, S.failed, S.cancelled},
    S.scheduled: {S.published, S.failed, S.cancelled},
    S.blocked: {S.draft, S.validated, S.ready, S.cancelled},
    S.failed: {S.draft, S.ready, S.cancelled},
    S.published: set(),
    S.cancelled: set(),
}
TRANSITIONS = {k.value if hasattr(k, "value") else k: {x.value for x in v} for k, v in TRANSITIONS.items()}


class InvalidTransition(RuntimeError):
    pass


class NeedsReconcile(RuntimeError):
    """La publicación está `unconfirmed`: hay que conciliar con el remoto antes de reenviar."""


def dedupe_key(channel_id: str, episode_id: str | None, clip_id: str | None, network: str, post_type: str,
               destination_id: str | None, content_version: int) -> str:
    raw = "|".join(str(x) for x in (channel_id, episode_id, clip_id, network, post_type, destination_id, content_version))
    return hashlib.sha256(raw.encode()).hexdigest()


def fingerprint(title: str | None, text: str | None) -> str:
    norm = " ".join(((title or "") + " " + (text or "")[:120]).lower().split())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


@dataclass
class Publication:
    row: dict

    def __getattr__(self, k: str) -> Any:
        try:
            return self.row[k]
        except KeyError as e:
            raise AttributeError(k) from e

    @property
    def content(self) -> dict:
        return json.loads(self.row["content_json"] or "{}")


class Ledger:
    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------------------ alta y lectura
    def prepare(self, *, channel_id: str, network: str, post_type: str, content_version: int, brand_id: str | None,
                destination_id: str | None, media_path: str | None, media_sha256: str | None, scheduled_at: str,
                timezone: str, content: dict, episode_id: str | None = None, clip_id: str | None = None) -> Publication:
        key = dedupe_key(channel_id, episode_id, clip_id, network, post_type, destination_id, content_version)
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM publications WHERE dedupe_key=?", (key,)).fetchone()
            if row:
                return Publication(dict(row))
            pub_id, now = new_id("pub"), utcnow_iso()
            c.execute(
                """INSERT INTO publications(pub_id, dedupe_key, channel_id, episode_id, clip_id, network, post_type,
                       brand_id, destination_id, media_path, media_sha256, content_version, scheduled_at, timezone,
                       state, content_json, content_fingerprint, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pub_id, key, channel_id, episode_id, clip_id, network, post_type, brand_id, destination_id, media_path,
                 media_sha256, content_version, scheduled_at, timezone, S.draft.value, json.dumps(content),
                 fingerprint(content.get("title"), content.get("text")), now, now),
            )
            self.db.event("publication", pub_id, "prepared", {"dedupe_key": key}, conn=c)
            return Publication(dict(c.execute("SELECT * FROM publications WHERE pub_id=?", (pub_id,)).fetchone()))

    def get(self, pub_id: str) -> Publication:
        r = self.db.one("SELECT * FROM publications WHERE pub_id=?", (pub_id,))
        if not r:
            raise KeyError(pub_id)
        return Publication(dict(r))

    def list(self, channel_id: str | None = None, states: list[str] | None = None) -> list[Publication]:
        sql, params = "SELECT * FROM publications WHERE 1=1", []
        if channel_id:
            sql += " AND channel_id=?"
            params.append(channel_id)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params += states
        return [Publication(dict(r)) for r in self.db.query(sql + " ORDER BY scheduled_at", tuple(params))]

    # ------------------------------------------------------------ estado
    def transition(self, pub_id: str, new: str, conn=None, **fields: Any) -> None:  # noqa: ANN001
        def _do(c) -> None:  # noqa: ANN001
            cur = c.execute("SELECT state FROM publications WHERE pub_id=?", (pub_id,)).fetchone()["state"]
            if new != cur and new not in TRANSITIONS[cur]:
                raise InvalidTransition(f"{pub_id}: {cur} → {new} no permitido")
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE publications SET state=?, updated_at=?{', ' + sets if sets else ''} WHERE pub_id=?",
                      (new, utcnow_iso(), *fields.values(), pub_id))
            self.db.event("publication", pub_id, f"state:{new}",
                          {k: v for k, v in fields.items() if k not in ("request_json", "response_json")}, conn=c)

        if conn is not None:
            _do(conn)
        else:
            with self.db.tx() as c:
                _do(c)

    def acquire(self, pub_id: str, owner: str, ttl_s: float = 600) -> bool:
        now = time.time()
        with self.db.tx() as c:
            r = c.execute("SELECT lock_owner, lock_expires_at FROM publications WHERE pub_id=?", (pub_id,)).fetchone()
            if r["lock_owner"] and r["lock_owner"] != owner and (r["lock_expires_at"] or 0) > now:
                return False
            c.execute("UPDATE publications SET lock_owner=?, lock_expires_at=? WHERE pub_id=?", (owner, now + ttl_s, pub_id))
            return True

    def release(self, pub_id: str, owner: str) -> None:
        self.db.execute("UPDATE publications SET lock_owner=NULL, lock_expires_at=NULL WHERE pub_id=? AND lock_owner=?",
                        (pub_id, owner))

    def mark_submitting(self, pub_id: str, owner: str, transport: str, request: dict) -> None:
        with self.db.tx() as c:
            r = c.execute("SELECT state, lock_owner FROM publications WHERE pub_id=?", (pub_id,)).fetchone()
            if r["lock_owner"] != owner:
                raise RuntimeError(f"{pub_id}: lock no pertenece a {owner}")
            if r["state"] == S.unconfirmed.value:
                raise NeedsReconcile(pub_id)
            self.transition(pub_id, S.submitting.value, conn=c, transport=transport,
                            request_json=json.dumps(request), last_attempt_at=utcnow_iso())
            c.execute("UPDATE publications SET attempts=attempts+1 WHERE pub_id=?", (pub_id,))

    def record_success(self, pub_id: str, response: dict, remote_id: str | None, planner_url: str | None = None) -> None:
        self.transition(pub_id, S.scheduled.value, remote_id=remote_id, planner_url=planner_url,
                        response_json=json.dumps(response), last_error=None)

    def record_handoff(self, pub_id: str, outbox_path: str) -> None:
        self.transition(pub_id, S.handoff.value, response_json=json.dumps({"outbox": outbox_path}))

    def record_ambiguous(self, pub_id: str, error: str, response: dict | None = None) -> None:
        self.transition(pub_id, S.unconfirmed.value, last_error=error[:2000],
                        response_json=json.dumps(response) if response else None)

    def record_failure(self, pub_id: str, error: str, blocked: bool = False) -> None:
        self.transition(pub_id, S.blocked.value if blocked else S.failed.value, last_error=error[:2000])

    def confirm_published(self, pub_id: str, final_url: str) -> None:
        if not final_url.startswith("https://"):
            raise ValueError("URL final no válida")
        self.transition(pub_id, S.published.value, final_url=final_url)

    # ------------------------------------------------------------ conciliación
    def reconcile(self, pub_id: str, remote_posts: list[dict]) -> str:
        """Concilia con la lista de posts programados del gestor (getScheduledPosts).

        Coincidencia: misma red + misma fecha/hora local + huella de título/texto.
        - encontrado → `scheduled` (con id remoto)
        - no encontrado y estaba `unconfirmed`/`handoff` → vuelve a `ready` (reenviar es seguro)
        - no encontrado y estaba `scheduled` con fecha pasada → se mantiene y se anota: hay que
          confirmar en la red (no se marca `published` sin evidencia).
        """
        pub = self.get(pub_id)
        content = pub.content
        local_dt = (pub.scheduled_at or "")[:19]
        match = None
        for post in remote_posts:
            nets = {p.get("network") for p in post.get("providers", [])}
            when = str((post.get("publicationDate") or {}).get("dateTime", ""))[:19]
            if pub.network not in nets or when != local_dt:
                continue
            title = (post.get("youtubeData") or post.get("tiktokData") or {}).get("title")
            if fingerprint(title, post.get("text")) == pub.content_fingerprint or (
                    title and title == content.get("title")):
                match = post
                break
        if match:
            rid = str(match.get("id") or match.get("postId") or "") or None
            if pub.state != S.scheduled.value:
                self.transition(pub_id, S.scheduled.value, remote_id=rid, planner_url=match.get("plannerUrl"),
                                response_json=json.dumps(match))
            return "scheduled"
        if pub.state in (S.unconfirmed.value, S.handoff.value):
            self.transition(pub_id, S.ready.value, last_error="conciliado: no existe en remoto; reenviable")
            return "ready"
        if pub.state == S.scheduled.value:
            self.db.event("publication", pub_id, "reconcile:missing_from_scheduled",
                          {"note": "ya no figura como programado; confirmar publicación en la red"})
            return "verify_on_network"
        return pub.state
