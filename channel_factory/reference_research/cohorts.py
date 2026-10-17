"""Rendimiento relativo por cohortes (formato + edad), con fórmula explícita.

- `views_per_day` = visitas / días desde publicación: PROMEDIO HISTÓRICO, no velocidad actual.
- `relative` = views_per_day / mediana de su cohorte (mismo formato y franja de edad).
- Velocidad reciente solo con snapshots: Δvisitas / Δdías entre dos fechas.
Correlación ≠ causalidad: estas cifras sirven para elegir qué analizar a fondo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median

AGE_BUCKETS = [(0, 30, "0-30d"), (30, 90, "30-90d"), (90, 365, "90-365d"), (365, 10**6, "365d+")]

FORMULA = ("relative = (views / days_since_publish) / median(views / days_since_publish) de la cohorte "
           "(mismo formato, misma franja de edad); cohortes con < 3 videos no se puntúan")


def _days(published_at: str, now: datetime) -> float:
    dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return max((now - dt).total_seconds() / 86400, 1.0)


def score(videos: list[dict], now: datetime | None = None, min_cohort: int = 3) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = []
    for v in videos:
        if v.get("views") is None or not v.get("published_at"):
            rows.append({**v, "views_per_day": None, "cohort": None, "relative": None})
            continue
        d = _days(v["published_at"], now)
        bucket = next(lbl for lo, hi, lbl in AGE_BUCKETS if lo <= d < hi)
        rows.append({**v, "age_days": round(d, 1), "views_per_day": v["views"] / d,
                     "cohort": f"{v.get('format', 'unknown')}|{bucket}"})
    groups: dict[str, list[float]] = {}
    for r in rows:
        if r.get("cohort"):
            groups.setdefault(r["cohort"], []).append(r["views_per_day"])
    for r in rows:
        g = groups.get(r.get("cohort") or "", [])
        r["relative"] = round(r["views_per_day"] / median(g), 3) if len(g) >= min_cohort and median(g) > 0 else None
    return rows


def pick_sample(rows: list[dict], top: int = 6, recent: int = 4, middle: int = 3, weak: int = 3) -> list[dict]:
    """Muestra para análisis profundo: populares, recientes, medianos y flojos (sin repetir)."""
    scored = [r for r in rows if r.get("relative") is not None]
    by_rel = sorted(scored, key=lambda r: -r["relative"])
    by_date = sorted([r for r in rows if r.get("published_at")], key=lambda r: r["published_at"], reverse=True)
    chosen, seen = [], set()

    def take(seq: list[dict], n: int, why: str) -> None:
        for r in seq:
            if len([c for c in chosen if c["why"] == why]) >= n:
                break
            if r["video_id"] not in seen:
                seen.add(r["video_id"])
                chosen.append({**r, "why": why})

    take(by_rel, top, "popular")
    take(by_date, recent, "reciente")
    mid = len(by_rel) // 2
    take(by_rel[max(0, mid - middle):mid + middle], middle, "mediano")
    take(list(reversed(by_rel)), weak, "flojo")
    return chosen


def snapshot_velocity(snapshots: list[dict]) -> dict[str, float | None]:
    """Velocidad reciente por video desde snapshots [{video_id, as_of, views}]."""
    by: dict[str, list[dict]] = {}
    for s in snapshots:
        by.setdefault(s["video_id"], []).append(s)
    out = {}
    for vid, ss in by.items():
        ss = sorted(ss, key=lambda x: x["as_of"])
        if len(ss) < 2 or ss[-1]["views"] is None or ss[-2]["views"] is None:
            out[vid] = None
            continue
        a, b = ss[-2], ss[-1]
        days = (datetime.fromisoformat(b["as_of"]) - datetime.fromisoformat(a["as_of"])).total_seconds() / 86400
        out[vid] = (b["views"] - a["views"]) / days if days > 0 else None
    return out
