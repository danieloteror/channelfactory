"""Programación del lote: ledger por pieza/destino, medios públicos y envío según el modo."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..db import DB
from ..editorial.planner import clip_slots
from ..models import EpisodeState
from ..orchestrator.artifacts import Artifacts, sha256_file
from ..orchestrator.worker import default_worker_id
from .ledger import Ledger
from .publisher import PublishBlocked, submit
from .storage import StorageBlocked, get_storage
from .transports import get_transport
from .validate import load_brand_snapshot


def _pieces(settings, channel_id: str, ep_row) -> list[dict]:  # noqa: ANN001
    from ..production.episode import EpisodeWorkspace

    prof = settings.channel(channel_id)
    pcfg = settings.cfg.publishing
    ws = EpisodeWorkspace.for_episode(settings, channel_id, ep_row["episode_id"])
    m = ws.load()
    slot = datetime.fromisoformat(ep_row["slot_at"])
    base = {"made_for_kids": prof.publishing.made_for_kids, "ai_disclosure": prof.publishing.synthetic_content_disclosure,
            "tags": m.tags}
    pieces = []
    master = ws.render_dir / "master_clean.mp4"
    if pcfg.youtube_long and master.exists():
        pieces.append({"network": "youtube", "post_type": "youtube_video", "clip_id": None, "media": master,
                       "when": slot, "content": {**base, "title": m.title, "text": m.description or m.title,
                                                 "thumbnail": str(ws.render_dir / "thumbnail.jpg")},
                       "version": m.script_version})
    val = ws.work / "clips" / "clip_validation.json"
    if val.exists():
        rep = json.loads(val.read_text())
        clips = [c for c in rep["result"]["clips"] if c["clip_id"] in rep["accepted"]]
        for c, when in zip(clips, clip_slots(slot, len(clips))):
            content = {**base, "title": c["hook"][:95], "text": c["hook"]}
            if pcfg.youtube_shorts:
                pieces.append({"network": "youtube", "post_type": "youtube_short", "clip_id": c["clip_id"],
                               "media": Path(c["file"]), "when": when, "content": content, "version": m.script_version})
            if pcfg.tiktok_clips:
                pieces.append({"network": "tiktok", "post_type": "tiktok_video", "clip_id": c["clip_id"],
                               "media": Path(c["file"]), "when": when, "content": content, "version": m.script_version})
    return pieces


DISABLED_MSG = ("Publicación desactivada (publishing.enabled=false). Los masters, clips, subtítulos y miniaturas "
                "quedan en channels/<canal>/episodes/<episodio>/render para subirlos a mano. Para automatizar con "
                "Metricool (recomendado) sigue docs/metricool.md.")


def schedule_month(settings, db: DB, channel_id: str, month: str) -> dict:  # noqa: ANN001
    if not settings.publishing_enabled:
        return {"disabled": DISABLED_MSG}
    prof = settings.channel(channel_id)
    mode = settings.effective_publish_mode(channel_id)
    led, art = Ledger(db), Artifacts(db)
    b = db.one("SELECT batch_id FROM batches WHERE channel_id=? AND month=?", (channel_id, month))
    if not b:
        return {"error": "no hay lote para ese mes"}
    eps = db.query("SELECT * FROM episodes WHERE batch_id=? AND state IN (?,?,?) ORDER BY slot_at",
                   (b["batch_id"], EpisodeState.qa.value, EpisodeState.clipping.value,
                    EpisodeState.ready_to_schedule.value))
    try:
        storage = get_storage(settings.cfg.publishing.media_storage)
    except StorageBlocked as e:
        storage, storage_err = None, str(e)
    else:
        storage_err = None
    transport = get_transport(settings.cfg.publishing.transport, settings.state_dir)
    brands = load_brand_snapshot(settings.state_dir / "metricool_brands.json")
    owner = default_worker_id()
    summary = {"mode": mode, "transport": transport.name, "results": []}
    for ep in eps:
        for p in _pieces(settings, channel_id, ep):
            dest = prof.publishing.youtube_destination_id if p["network"] == "youtube" else prof.publishing.tiktok_destination_id
            pub = led.prepare(channel_id=channel_id, network=p["network"], post_type=p["post_type"],
                              content_version=p["version"], brand_id=prof.publishing.metricool_brand_id,
                              destination_id=dest, media_path=str(p["media"]), media_sha256=sha256_file(p["media"]),
                              scheduled_at=p["when"].isoformat(), timezone=prof.publishing.timezone,
                              content=p["content"], episode_id=ep["episode_id"], clip_id=p["clip_id"])
            art.protect(str(p["media"]), p["when"].isoformat())
            entry = {"pub_id": pub.pub_id, "episode": ep["episode_id"], "type": p["post_type"], "when": p["when"].isoformat()}
            if not pub.media_url and storage is not None and mode != "preview":
                try:
                    url = storage.publish(p["media"], f"{channel_id}/{ep['episode_id']}/{p['media'].name}")
                    db.execute("UPDATE publications SET media_url=? WHERE pub_id=?", (url, pub.pub_id))
                except StorageBlocked as e:
                    entry["storage"] = str(e)
            elif storage_err:
                entry["storage"] = storage_err
            try:
                entry["state"] = submit(led, pub.pub_id, transport, owner=owner, profile=prof, mode=mode,
                                        protected_brand_ids=settings.cfg.protected_brand_ids, brands=brands)
            except PublishBlocked as e:
                entry["state"], entry["blocked"] = "blocked", str(e)
            summary["results"].append(entry)
    return summary
