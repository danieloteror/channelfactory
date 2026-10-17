"""Planificación mensual idempotente: lote único por canal+mes, slots y episodios.

Planificar un mes no crea recurrencia: la repetición mensual es opción explícita.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from ..db import DB, new_id, utcnow_iso
from ..models import EpisodeState

DEFAULT_WEEKDAYS = (1, 3, 5)  # mar, jue, sáb
DEFAULT_HOUR = (14, 0)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def month_slots(month: str, n: int, tz: str, weekdays=DEFAULT_WEEKDAYS, hour=DEFAULT_HOUR) -> list[datetime]:  # noqa: ANN001
    """`n` fechas repartidas en el mes, en los días preferidos, en la zona del canal."""
    y, m = (int(x) for x in month.split("-"))
    days = [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]
    pref = [d for d in days if d.weekday() in weekdays]
    pool = pref if len(pref) >= n else days
    if n <= 0:
        return []
    if n >= len(pool):
        chosen = pool[:n]
    else:
        step = len(pool) / n
        chosen = [pool[int(i * step)] for i in range(n)]
    return [datetime.combine(d, time(*hour), tzinfo=ZoneInfo(tz)) for d in chosen]


def load_backlog(channel_dir: Path) -> list[dict]:
    p = channel_dir / "backlog.yaml"
    if not p.exists():
        return []
    return yaml.safe_load(p.read_text(encoding="utf-8")).get("topics", [])


def load_published(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {slugify(t) for t in data.get("topics", [])}


def plan_month(db: DB, *, channel_id: str, month: str, tz: str, long_videos: int, channel_dir: Path,
               published_file: Path | None, banned: list[str]) -> dict:
    """Crea o recupera el lote del mes. Repetir el comando devuelve el mismo lote."""
    existing = db.one("SELECT * FROM batches WHERE channel_id=? AND month=?", (channel_id, month))
    if existing:
        return {"batch_id": existing["batch_id"], "created": False, **json.loads(existing["plan_json"] or "{}")}
    backlog = load_backlog(channel_dir)
    published = load_published(published_file)
    banned_l = [b.lower() for b in banned]
    used = {r["slug"] for r in db.query("SELECT slug FROM episodes WHERE channel_id=?", (channel_id,))}
    candidates = []
    for t in backlog:
        slug = slugify(t["title"])
        if slug in published or slug in used:
            continue
        if any(b in (t.get("title", "") + " " + t.get("angle", "")).lower() for b in banned_l):
            continue
        candidates.append({**t, "slug": slug})
    # Variedad: no dos episodios seguidos del mismo pilar temático.
    ordered, last = [], None
    pool = list(candidates)
    while pool and len(ordered) < long_videos:
        pick = next((c for c in pool if c.get("pillar") != last), pool[0])
        pool.remove(pick)
        ordered.append(pick)
        last = pick.get("pillar")
    slots = month_slots(month, len(ordered), tz)
    batch_id = new_id("batch")
    plan = {"month": month, "timezone": tz, "requested": long_videos, "planned": len(ordered),
            "shortfall_reason": None if len(ordered) == long_videos else
            f"backlog insuficiente: {len(candidates)} temas disponibles (añade temas a backlog.yaml)",
            "episodes": []}
    now = utcnow_iso()
    with db.tx() as c:
        c.execute("INSERT INTO batches(batch_id, channel_id, month, status, plan_json, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?)", (batch_id, channel_id, month, "planned", "{}", now, now))
        for i, (topic, slot) in enumerate(zip(ordered, slots), 1):
            ep_id = f"{month}-{i:02d}-{topic['slug'][:40]}"
            c.execute("INSERT INTO episodes(episode_id, channel_id, batch_id, slug, title, state, slot_at, data_json, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (ep_id, channel_id, batch_id, topic["slug"], topic["title"], EpisodeState.planned.value,
                       slot.isoformat(), json.dumps(topic), now, now))
            plan["episodes"].append({"episode_id": ep_id, "title": topic["title"], "slot": slot.isoformat(),
                                     "pillar": topic.get("pillar")})
        c.execute("UPDATE batches SET plan_json=? WHERE batch_id=?", (json.dumps(plan), batch_id))
        db.event("batch", batch_id, "planned", {"episodes": len(ordered)}, conn=c)
    return {"batch_id": batch_id, "created": True, **plan}


def clip_slots(long_slot: datetime, n: int, gap_hours: int = 24) -> list[datetime]:
    """Clips DESPUÉS del largo (no prometen un largo aún no disponible)."""
    return [long_slot + timedelta(hours=gap_hours * (i + 1)) for i in range(n)]
