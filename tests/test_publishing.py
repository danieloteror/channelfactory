"""Riesgos 1, 2, 3, 5, 11 y 12 en publicación."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from channel_factory.db import DB
from channel_factory.publishing.ledger import Ledger, NeedsReconcile
from channel_factory.publishing.publisher import PublishBlocked, record_result, submit
from channel_factory.publishing.storage import verify_public_url
from channel_factory.publishing.transports import (HttpApiTransport, McpHandoffTransport, SendResult,
                                                   TransportAmbiguous, TransportBlocked)
from channel_factory.publishing.validate import validate

from .conftest import BRAND, OTHER_BRAND, make_video

TZ = "America/New_York"


def _when(days: int = 3) -> str:
    return (datetime.now(ZoneInfo(TZ)) + timedelta(days=days)).replace(microsecond=0, second=0).isoformat()


def _pub(led: Ledger, media, *, post_type="youtube_short", network="youtube", brand=BRAND, version=1,  # noqa: ANN001
         clip="c1", url="https://cdn.example.com/c1.mp4"):
    from channel_factory.orchestrator.artifacts import sha256_file

    p = led.prepare(channel_id="testchan", network=network, post_type=post_type, content_version=version,
                    brand_id=brand, destination_id="UC_TEST" if network == "youtube" else "tt_test",
                    media_path=str(media), media_sha256=sha256_file(media), scheduled_at=_when(), timezone=TZ,
                    content={"title": "A clear title", "text": "Description", "made_for_kids": False},
                    episode_id="ep1", clip_id=clip)
    led.db.execute("UPDATE publications SET media_url=? WHERE pub_id=?", (url, p.pub_id))
    return led.get(p.pub_id)


class FakeTransport:
    name = "fake"

    def __init__(self, mode: str = "ok"):
        self.mode, self.sent = mode, []

    def send(self, pub_id, blog_id, date_iso, info):  # noqa: ANN001, ANN201
        self.sent.append(pub_id)
        if self.mode == "timeout":
            raise TransportAmbiguous("timeout tras enviar")
        if self.mode == "blocked":
            raise TransportBlocked("401")
        return SendResult("scheduled", remote_id="r-1", planner_url="https://app.metricool.com/x", response={"ok": 1})


def _submit(led, pub, tr, settings, brands, owner="w1"):  # noqa: ANN001, ANN202
    return submit(led, pub.pub_id, tr, owner=owner, profile=settings.channel("testchan"), mode="autopilot",
                  protected_brand_ids=[OTHER_BRAND], brands=brands, check_url=False)


@pytest.fixture
def vertical(tmp_path):  # noqa: ANN001, ANN201
    return make_video(tmp_path / "v.mp4", 540, 960, 4.0)


def test_prepare_dedupes(db: DB, vertical) -> None:  # noqa: ANN001
    led = Ledger(db)
    a, b = _pub(led, vertical), _pub(led, vertical)
    assert a.pub_id == b.pub_id
    c = _pub(led, vertical, version=2)  # nueva versión de contenido = nueva pieza
    assert c.pub_id != a.pub_id


def test_success_and_no_double_send(db: DB, vertical, settings, brands) -> None:  # noqa: ANN001
    led, tr = Ledger(db), FakeTransport()
    pub = _pub(led, vertical)
    assert _submit(led, pub, tr, settings, brands) == "scheduled"
    assert _submit(led, pub, tr, settings, brands) == "scheduled"  # idempotente
    assert tr.sent == [pub.pub_id]
    assert led.get(pub.pub_id).remote_id == "r-1"


def test_timeout_requires_reconcile(db: DB, vertical, settings, brands) -> None:  # noqa: ANN001
    led = Ledger(db)
    pub = _pub(led, vertical)
    assert _submit(led, pub, FakeTransport("timeout"), settings, brands) == "unconfirmed"
    tr = FakeTransport()
    with pytest.raises(NeedsReconcile):
        _submit(led, pub, tr, settings, brands)
    assert tr.sent == []  # nunca reenvía a ciegas
    p = led.get(pub.pub_id)
    remote = [{"id": 99, "providers": [{"network": "youtube"}], "text": "Description",
               "publicationDate": {"dateTime": p.scheduled_at[:19], "timezone": TZ},
               "youtubeData": {"title": "A clear title"}}]
    assert led.reconcile(pub.pub_id, remote) == "scheduled"
    assert led.get(pub.pub_id).remote_id == "99"


def test_timeout_then_absent_remote_becomes_resendable(db: DB, vertical, settings, brands) -> None:  # noqa: ANN001
    led = Ledger(db)
    pub = _pub(led, vertical)
    _submit(led, pub, FakeTransport("timeout"), settings, brands)
    assert led.reconcile(pub.pub_id, []) == "ready"
    assert _submit(led, pub, FakeTransport(), settings, brands) == "scheduled"


def test_two_workers_cannot_submit_same_piece(tmp_path, vertical, settings, brands) -> None:  # noqa: ANN001
    path = tmp_path / "p.sqlite"
    pub = _pub(Ledger(DB(path)), vertical)
    sent: list[str] = []

    class Slow(FakeTransport):
        def send(self, *a):  # noqa: ANN002, ANN201
            import time

            time.sleep(0.3)
            sent.append("x")
            return super().send(*a)

    results = []

    def go(n: int) -> None:
        results.append(_submit(Ledger(DB(path)), pub, Slow(), settings, brands, owner=f"w{n}"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(sent) == 1
    assert results.count("scheduled") >= 1 and set(results) <= {"scheduled", "locked", "submitting"}


@pytest.mark.parametrize("brand,network,expect", [
    (OTHER_BRAND, "youtube", "protegida"),
    ("1000003", "youtube", "≠ marca del perfil"),
])
def test_wrong_brand_blocked_before_send(db, vertical, settings, brands, brand, network, expect) -> None:  # noqa: ANN001
    led, tr = Ledger(db), FakeTransport()
    pub = _pub(led, vertical, brand=brand, network=network)
    with pytest.raises(PublishBlocked, match=expect):
        _submit(led, pub, tr, settings, brands)
    assert tr.sent == [] and led.get(pub.pub_id).state == "blocked"


def test_brand_without_network_blocked(db, vertical, settings) -> None:  # noqa: ANN001
    brands = [{"id": int(BRAND), "label": "testchan", "networksData": {}}]
    led, tr = Ledger(db), FakeTransport()
    pub = _pub(led, vertical)
    with pytest.raises(PublishBlocked, match="no tiene youtube conectado"):
        _submit(led, pub, tr, settings, brands)
    assert tr.sent == []


def test_bad_media_never_ready(db, tmp_path, settings, brands) -> None:  # noqa: ANN001
    led = Ledger(db)
    prof = settings.channel("testchan")
    horiz = make_video(tmp_path / "h.mp4", 1280, 720, 3.0)
    mute = make_video(tmp_path / "m.mp4", 540, 960, 3.0, audio=False)
    cases = {
        "requiere vertical": _pub(led, horiz, clip="h"),
        "no tiene audio": _pub(led, mute, clip="m"),
    }
    missing = _pub(led, horiz, clip="x")
    db.execute("UPDATE publications SET media_path=? WHERE pub_id=?", (str(tmp_path / "nope.mp4"), missing.pub_id))
    cases["sin archivo de video"] = led.get(missing.pub_id)
    for needle, pub in cases.items():
        v = validate(pub, profile=prof, protected_brand_ids=[OTHER_BRAND], brands=brands, mode="autopilot",
                     check_url=False)
        assert not v.ok and any(needle in p for p in v.problems), (needle, v.problems)
        assert led.get(pub.pub_id).state == "draft"


def test_preview_mode_never_sends(db, vertical, settings, brands) -> None:  # noqa: ANN001
    led, tr = Ledger(db), FakeTransport()
    pub = _pub(led, vertical)
    with pytest.raises(PublishBlocked, match="preview"):
        submit(led, pub.pub_id, tr, owner="w", profile=settings.channel("testchan"), mode="preview",
               protected_brand_ids=[], brands=brands, check_url=False)
    assert tr.sent == []


def test_inaccessible_or_expiring_url_rejected() -> None:
    assert not verify_public_url("http://example.com/a.mp4").ok
    chk = verify_public_url("https://127.0.0.1:9/a.mp4", timeout=2)
    assert not chk.ok and any("no accesible" in p for p in chk.problems)
    signed = ("https://bucket.s3.amazonaws.com/a.mp4?X-Amz-Date=20260101T000000Z&X-Amz-Expires=3600"
              "&X-Amz-Signature=abc")
    assert any("caduca" in p for p in verify_public_url(signed, timeout=2).problems)


def test_http_transport_without_token_is_blocked(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("METRICOOL_USER_TOKEN", raising=False)
    with pytest.raises(TransportBlocked):
        HttpApiTransport().send("p", BRAND, "2030-01-01T10:00:00-05:00", {})


def test_mcp_handoff_roundtrip(db, vertical, settings, brands, tmp_path) -> None:  # noqa: ANN001
    led = Ledger(db)
    pub = _pub(led, vertical)
    tr = McpHandoffTransport(tmp_path / "outbox")
    assert _submit(led, pub, tr, settings, brands) == "handoff"
    req = json.loads((tmp_path / "outbox" / f"{pub.pub_id}.json").read_text())
    assert req["tool"] == "createScheduledPost" and req["arguments"]["blogId"] == BRAND
    info = json.loads(req["arguments"]["info"])
    assert info["youtubeData"]["type"] == "short" and info["youtubeData"]["madeForKids"] is False
    assert info["autoPublish"] is True and info["draft"] is False
    assert record_result(led, pub.pub_id, {"data": {"id": 7, "plannerUrl": "https://app.metricool.com/p/7"}}) == "scheduled"
    with pytest.raises(ValueError):
        led.confirm_published(pub.pub_id, "not-a-url")
