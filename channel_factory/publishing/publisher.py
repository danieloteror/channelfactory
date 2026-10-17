"""Orquestación de un envío: lock → validación → envío → registro → liberación."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..models import PublicationState as S
from .ledger import Ledger, NeedsReconcile
from .payload import build_info
from .transports import SendResult, TransportAmbiguous, TransportBlocked, TransportPermanent
from .validate import Validation, validate


class PublishBlocked(RuntimeError):
    pass


def make_info(pub, profile, mode: str) -> dict:  # noqa: ANN001
    c = pub.content
    when = datetime.fromisoformat(pub.scheduled_at)
    return build_info(
        post_type=pub.post_type, text=c["text"], title=c["title"], media_url=pub.media_url,
        scheduled_local=when, timezone=pub.timezone, mode=mode, made_for_kids=bool(c.get("made_for_kids")),
        ai_disclosure=bool(c.get("ai_disclosure", profile.publishing.synthetic_content_disclosure)),
        category=c.get("category", profile.publishing.youtube_category), tags=c.get("tags"),
        privacy=c.get("privacy", "public"), thumbnail_url=c.get("thumbnail_url"),
    )


def mark_ready(ledger: Ledger, pub_id: str, v: Validation) -> None:
    pub = ledger.get(pub_id)
    if not v.ok:
        if pub.state not in (S.blocked.value,):
            ledger.transition(pub_id, S.blocked.value, last_error="; ".join(v.problems)[:2000])
        raise PublishBlocked("; ".join(v.problems))
    if pub.state in (S.draft.value, S.blocked.value):
        ledger.transition(pub_id, S.validated.value, last_error=None)
    if ledger.get(pub_id).state == S.validated.value:
        ledger.transition(pub_id, S.ready.value)


def submit(ledger: Ledger, pub_id: str, transport, *, owner: str, profile, mode: str,  # noqa: ANN001
           protected_brand_ids: list[str], brands: list[dict], check_url: bool = True) -> str:
    """Devuelve el estado final. Nunca reenvía una publicación `unconfirmed` sin conciliar."""
    if not ledger.acquire(pub_id, owner):
        return "locked"
    try:
        pub = ledger.get(pub_id)
        if pub.state == S.unconfirmed.value:
            raise NeedsReconcile(f"{pub_id}: conciliar con getScheduledPosts antes de reenviar")
        if pub.state in (S.scheduled.value, S.published.value, S.handoff.value, S.submitting.value):
            return pub.state  # ya enviado o en curso: idempotente
        v = validate(pub, profile=profile, protected_brand_ids=protected_brand_ids, brands=brands, mode=mode,
                     check_url=check_url)
        mark_ready(ledger, pub_id, v)
        pub = ledger.get(pub_id)
        info = make_info(pub, profile, mode)
        when = datetime.fromisoformat(pub.scheduled_at)
        date_iso = (when if when.tzinfo else when.replace(tzinfo=ZoneInfo(pub.timezone))).isoformat()
        ledger.mark_submitting(pub_id, owner, transport.name,
                               {"blogId": pub.brand_id, "date": date_iso, "info": info})
        try:
            res: SendResult = transport.send(pub_id, pub.brand_id, date_iso, info)
        except TransportAmbiguous as e:
            ledger.record_ambiguous(pub_id, str(e))
            return S.unconfirmed.value
        except TransportBlocked as e:
            ledger.record_failure(pub_id, str(e), blocked=True)
            return S.blocked.value
        except TransportPermanent as e:
            ledger.record_failure(pub_id, str(e))
            return S.failed.value
        if res.kind == "handoff":
            ledger.record_handoff(pub_id, res.outbox_path or "")
            return S.handoff.value
        ledger.record_success(pub_id, res.response or {}, res.remote_id, res.planner_url)
        return S.scheduled.value
    finally:
        ledger.release(pub_id, owner)


def record_result(ledger: Ledger, pub_id: str, response: dict) -> str:
    """Registra la respuesta que la sesión obtuvo al ejecutar la solicitud MCP."""
    pub = ledger.get(pub_id)
    if pub.state != S.handoff.value:
        raise RuntimeError(f"{pub_id} está en {pub.state}, no en handoff")
    if response.get("error") or response.get("isError"):
        ledger.record_failure(pub_id, str(response.get("error") or response)[:2000])
        return S.failed.value
    data = response.get("data", response)
    post = data[0] if isinstance(data, list) and data else data
    rid = post.get("id") if isinstance(post, dict) else None
    planner = post.get("plannerUrl") if isinstance(post, dict) else None
    if rid is None and planner is None:
        ledger.record_ambiguous(pub_id, "respuesta sin id ni plannerUrl", response)
        return S.unconfirmed.value
    ledger.record_success(pub_id, response, str(rid) if rid else None, planner)
    return S.scheduled.value
