"""Análisis de canales y videos de referencia → channel_analysis.json + informe base.

Separa observación (dato medido con su fuente), hipótesis y confianza. Las
conclusiones de estilo visual las completa un modelo con visión o una persona
sobre los keyframes extraídos: una transcripción no demuestra análisis visual.
Nunca se inventan CTR, retención, RPM ni ingresos de un canal ajeno.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import av_analysis, cohorts, youtube_api, ytdlp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def analyze_channel(url: str, out_dir: Path, *, catalog_limit: int = 500, deep_sample: int = 10,
                    download_media: bool = False, probe_shorts: bool = True, db=None) -> dict:  # noqa: ANN001
    out_dir.mkdir(parents=True, exist_ok=True)
    limitations: list[str] = []
    chan = youtube_api.resolve_channel(url)
    ids = youtube_api.list_uploads(chan["uploads_playlist"], catalog_limit)
    if chan.get("videos") and len(ids) < chan["videos"]:
        limitations.append(f"catálogo muestreado: {len(ids)} de {chan['videos']} videos (límite {catalog_limit})")
    vids = youtube_api.videos(ids)
    for v in vids:
        probe = youtube_api.probe_short(v["video_id"]) if (probe_shorts and (v.get("duration_s") or 999) <= 180) else None
        v["format"], v["format_method"] = youtube_api.classify_format(v, probe)
    if any(v["format"] == "short_candidate" for v in vids):
        limitations.append("algunos videos ≤180 s no pudieron confirmarse como Shorts (sin acceso a youtube.com)")
    rows = cohorts.score(vids)
    if db is not None:
        for v in vids:
            db.execute("INSERT OR IGNORE INTO ref_snapshots(channel_ref, video_id, as_of, views, likes, comments, source) "
                       "VALUES (?,?,?,?,?,?,?)", (chan["channel_id"], v["video_id"], chan["retrieved_at"], v["views"],
                                                   v["likes"], v["comments"], "youtube_data_api_v3"))
    sample = cohorts.pick_sample(rows, top=max(1, deep_sample // 2), recent=max(1, deep_sample // 4),
                                 middle=2, weak=2)[:deep_sample]
    deep = []
    for s in sample:
        vurl = f"https://www.youtube.com/watch?v={s['video_id']}"
        vdir = out_dir / "videos" / s["video_id"]
        entry = {"video_id": s["video_id"], "title": s["title"], "why": s["why"], "relative": s.get("relative")}
        try:
            subs = ytdlp.subtitles(vurl, vdir / "subs")
            entry["subtitles"] = [str(p) for p in subs]
            text = ytdlp.vtt_to_text(subs[0]) if subs else None
        except (ytdlp.YtDlpBlocked, RuntimeError) as e:
            entry["subtitles_error"] = str(e)[:300]
            text = None
        if download_media:
            try:
                media = ytdlp.download_for_analysis(vurl, vdir / "media")
                entry["av"] = av_analysis.analyze_file(media, vdir, transcript=text)
            except (ytdlp.YtDlpBlocked, RuntimeError) as e:
                entry["av_error"] = str(e)[:300]
        deep.append(entry)
    if not download_media:
        limitations.append("sin descarga de medios: no hay análisis de imagen/audio (usa --download-media si el uso está permitido)")
    result = {
        "schema_version": "1.0", "retrieved_at": _now(), "source_url": url, "channel": chan,
        "access_limitations": limitations, "cohort_formula": cohorts.FORMULA,
        "videos": [{k: r.get(k) for k in ("video_id", "title", "published_at", "duration_s", "format", "format_method",
                                          "views", "likes", "comments", "age_days", "views_per_day", "relative",
                                          "thumbnail")} for r in rows],
        "deep_sample": deep,
        "findings": [],  # {aspect, observation, evidence[], hypothesis, confidence} — a completar con revisión visual
        "private_metrics": "no disponibles: CTR/retención/ingresos requieren YouTube Analytics del propietario",
    }
    (out_dir / "channel_analysis.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    write_report(result, out_dir / "reference_report.md")
    return result


def analyze_video(src: str, out_dir: Path, *, download_media: bool = False) -> dict:
    """Mismo análisis audiovisual para un video suelto (URL o archivo local)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = Path(src)
    text = None
    if p.exists():
        media = p
        meta = {"source": str(p)}
    else:
        meta = ytdlp.metadata(src)
        meta = {k: meta.get(k) for k in ("id", "title", "upload_date", "duration", "view_count", "like_count",
                                         "comment_count", "channel", "channel_id")}
        try:
            subs = ytdlp.subtitles(src, out_dir / "subs")
            text = ytdlp.vtt_to_text(subs[0]) if subs else None
        except (ytdlp.YtDlpBlocked, RuntimeError):
            pass
        if not download_media:
            res = {"meta": meta, "transcript_words": len(text.split()) if text else None,
                   "limitations": ["sin descarga de medios: análisis audiovisual pendiente"]}
            (out_dir / "video_analysis.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
            return res
        media = ytdlp.download_for_analysis(src, out_dir / "media")
    res = {"meta": meta, "av": av_analysis.analyze_file(media, out_dir, transcript=text), "retrieved_at": _now()}
    (out_dir / "video_analysis.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
    return res


def write_report(a: dict, path: Path) -> None:
    ch = a["channel"]
    vids = [v for v in a["videos"] if v.get("relative") is not None]
    top = sorted(vids, key=lambda v: -v["relative"])[:10]
    lines = [
        f"# Análisis de referencia: {ch['title']}",
        "",
        f"Consultado: {a['retrieved_at']} · Fuente: YouTube Data API v3 · Canal `{ch['channel_id']}`",
        "",
        "## Datos públicos (observación)",
        f"- Suscriptores: {ch['subscribers'] if ch['subscribers'] is not None else 'ocultos / no disponibles'}",
        f"- Visualizaciones totales: {ch['views']}",
        f"- Videos: {ch['videos']} (analizados en catálogo: {len(a['videos'])})",
        "",
        "## Rendimiento relativo",
        f"Fórmula: {a['cohort_formula']}. Promedio histórico, no velocidad actual.",
        "",
        "| Video | Formato | Edad (d) | Visitas | Relativo |",
        "|---|---|---|---|---|",
        *[f"| {v['title'][:70]} | {v['format']} | {v.get('age_days')} | {v['views']} | {v['relative']} |" for v in top],
        "",
        "## Limitaciones",
        *[f"- {x}" for x in a["access_limitations"]],
        "",
        "## Hallazgos (a completar con revisión visual de keyframes)",
        "Para cada conclusión: **Observación** (dato + evidencia) · **Hipótesis** · **Confianza** (baja/media/alta).",
        "No confundir correlación con causalidad. No atribuir el canal a herramientas de IA sin evidencia.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
