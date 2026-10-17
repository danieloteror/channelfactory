"""Configuración, planificación idempotente y claims."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from channel_factory.config import ChannelProfile
from channel_factory.editorial import claims as cl
from channel_factory.editorial.planner import month_slots, plan_month
from channel_factory.models import EpisodeManifest
from channel_factory.providers.tts_kokoro import normalize


def test_autopilot_requires_destinations() -> None:
    with pytest.raises(ValidationError):
        ChannelProfile.model_validate({"id": "x", "name": "x", "niche": "n", "publishing": {"mode": "autopilot"}})


def test_most_restrictive_mode_wins(settings) -> None:  # noqa: ANN001
    assert settings.cfg.publishing.mode == "autopilot"
    assert settings.effective_publish_mode("testchan") == "draft"


def test_plan_month_is_idempotent(db, tmp_path) -> None:  # noqa: ANN001
    d = tmp_path / "chan"
    d.mkdir()
    topics = [{"title": f"Topic {i}", "pillar": "a" if i % 2 else "b"} for i in range(6)] + [
        {"title": "Hot stock tips", "angle": "specific stock picks"}]
    (d / "backlog.yaml").write_text(yaml.safe_dump({"topics": topics}))
    (d / "published.yaml").write_text(yaml.safe_dump({"topics": ["Topic 0"]}))
    kw = dict(channel_id="c", month="2026-11", tz="America/New_York", long_videos=4, channel_dir=d,
              published_file=d / "published.yaml", banned=["stock picks"])
    a = plan_month(db, **kw)
    b = plan_month(db, **kw)
    assert a["created"] and not b["created"] and a["batch_id"] == b["batch_id"]
    titles = [e["title"] for e in a["episodes"]]
    assert len(titles) == 4 and "Topic 0" not in titles and "Hot stock tips" not in titles
    assert db.one("SELECT COUNT(*) n FROM episodes")["n"] == 4
    pillars = [t.split()[-1] for t in titles]
    assert all(int(x) % 2 != int(y) % 2 for x, y in zip(pillars, pillars[1:]))  # variedad de pilar


def test_month_slots_in_channel_timezone() -> None:
    s = month_slots("2026-11", 12, "America/New_York")
    assert len(s) == 12 and len({x.date() for x in s}) == 12
    assert all(x.tzinfo is not None and x.month == 11 for x in s)


def test_claims_rules(tmp_path) -> None:  # noqa: ANN001
    m = EpisodeManifest.model_validate({"channel_id": "c", "episode_id": "e", "title": "t", "scenes": [
        {"scene_id": "s1", "beat": "hook", "text": "Prices rose 9.1% in a year.", "claims": ["c1"]},
        {"scene_id": "s2", "beat": "example", "text": "Imagine a coffee jumps 24% overnight."}]})
    p = tmp_path / "claims.json"
    p.write_text(json.dumps({"claims": [{"claim_id": "c1", "text": "CPI-U rose 9.1%", "kind": "current_data",
                                         "sources": [{"url": "https://www.bls.gov/x"}], "scene_ids": ["s9"]}]}))
    probs = cl.check(cl.load(p), m)
    assert any("as_of" in x for x in probs)
    assert any("fecha de consulta" in x for x in probs)
    assert any("s9" in x for x in probs)
    assert any("s2" in x and "porcentajes" in x for x in probs)


def test_number_normalization() -> None:
    assert normalize("It rose 4.1% since 2008, to 1,200.") == \
        "It rose four point one percent since two thousand eight, to twelve hundred."


def test_publishing_disabled_by_default_never_sends(settings, db) -> None:  # noqa: ANN001
    from channel_factory.config import FactoryConfig, settings_from
    from channel_factory.publishing.cli_ops import schedule_month

    assert FactoryConfig().publishing.enabled is False
    off = settings_from(settings.root, settings.cfg.model_copy(
        update={"publishing": settings.cfg.publishing.model_copy(update={"enabled": False})}))
    assert off.effective_publish_mode("testchan") == "preview"
    assert "disabled" in schedule_month(off, db, "testchan", "2026-11")
