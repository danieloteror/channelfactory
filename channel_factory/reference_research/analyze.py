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

from . import av_analysis, cohorts, patterns, youtube_api, ytdlp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _channel_base(url: str) -> str:
    u = url.split("?")[0].rstrip("/")
    for tab in ("/videos", "/shorts", "/streams", "/featured"):
        if u.endswith(tab):
            u = u[: -len(tab)]
    return u


def catalog_ytdlp(url: str, limit: int = 500, enrich: int = 80) -> tuple[dict, list[dict], list[str]]:
    """Catálogo sin API key: pestañas /videos, /shorts y /streams del canal (formato fiable por pestaña)
    y metadatos completos (fecha, likes, comentarios) para los `enrich` videos más vistos y recientes."""
    base = _channel_base(url)
    notes = ["catálogo vía yt-dlp (sin YOUTUBE_API_KEY): formato según la pestaña del canal"]
    vids: dict[str, dict] = {}
    chan: dict = {"source": "yt-dlp", "retrieved_at": _now(), "subscribers": None, "views": None}
    for tab, fmt in (("videos", "long"), ("shorts", "short"), ("streams", "live")):
        try:
            data = ytdlp.playlist_json(f"{base}/{tab}", limit)
        except RuntimeError as e:
            notes.append(f"pestaña {tab}: {str(e)[:120]}")
            continue
        chan.update({k: v for k, v in {
            "channel_id": data.get("channel_id") or data.get("id"), "title": data.get("channel") or data.get("title"),
            "handle": data.get("uploader_id"), "subscribers": data.get("channel_follower_count"),
            "description": data.get("description")}.items() if v is not None})
        for e in data.get("entries") or []:
            if not e or not e.get("id"):
                continue
            vids[e["id"]] = {"video_id": e["id"], "title": e.get("title"), "published_at": None,
                             "duration_s": e.get("duration"), "views": e.get("view_count"), "likes": None,
                             "comments": None, "thumbnail": None, "is_live": fmt == "live",
                             "format": fmt, "format_method": f"pestaña /{tab}"}
    ranked = sorted(vids.values(), key=lambda v: -(v["views"] or 0))
    pick = {v["video_id"] for v in ranked[: enrich // 2]} | {v["video_id"] for v in list(vids.values())[: enrich // 2]}
    for vid in pick:
        try:
            m = ytdlp.metadata(f"https://www.youtube.com/watch?v={vid}")
        except (RuntimeError, ytdlp.YtDlpBlocked):
            continue
        d = m.get("upload_date")
        vids[vid].update(published_at=f"{d[:4]}-{d[4:6]}-{d[6:]}T00:00:00+00:00" if d else None,
                         likes=m.get("like_count"), comments=m.get("comment_count"),
                         views=m.get("view_count", vids[vid]["views"]), duration_s=m.get("duration") or vids[vid]["duration_s"],
                         thumbnail=m.get("thumbnail"), tags=m.get("tags") or [])
    if len(vids) > len(pick):
        notes.append(f"fecha de publicación solo para {len(pick)} videos enriquecidos: el resto no entra en cohortes")
    chan["videos"] = len(vids)
    return chan, list(vids.values()), notes


def _save_thumb(url: str | None, dest: Path) -> str | None:
    """Descarga la miniatura para revisión visual (modelo con visión o persona). Mejor esfuerzo."""
    import urllib.request

    if not url:
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 - URL de metadatos de YouTube
            dest.write_bytes(r.read())
        return str(dest)
    except OSError:
        return None


def analyze_channel(url: str, out_dir: Path, *, catalog_limit: int = 500, deep_sample: int = 10,
                    download_media: bool = False, probe_shorts: bool = True, enrich: int = 80,
                    db=None) -> dict:  # noqa: ANN001
    import os

    out_dir.mkdir(parents=True, exist_ok=True)
    limitations: list[str] = []
    if os.environ.get("YOUTUBE_API_KEY"):
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
    else:
        chan, vids, notes = catalog_ytdlp(url, catalog_limit, enrich)
        limitations += notes
        if not vids:
            raise ytdlp.YtDlpBlocked("sin catálogo (" + "; ".join(notes[1:])[:300] + "): configura YOUTUBE_API_KEY "
                                     "o instala yt-dlp con acceso a youtube.com")
    rows = cohorts.score(vids)
    if db is not None:
        for v in vids:
            db.execute("INSERT OR IGNORE INTO ref_snapshots(channel_ref, video_id, as_of, views, likes, comments, source) "
                       "VALUES (?,?,?,?,?,?,?)", (chan.get("channel_id") or url, v["video_id"], chan["retrieved_at"],
                                                   v["views"], v["likes"], v["comments"], chan.get("source", "?")))
    sample = cohorts.pick_sample(rows, top=max(1, deep_sample // 2), recent=max(1, deep_sample // 4),
                                 middle=2, weak=2)[:deep_sample]
    deep = []
    for s in sample:
        vurl = f"https://www.youtube.com/watch?v={s['video_id']}"
        vdir = out_dir / "videos" / s["video_id"]
        entry = {"video_id": s["video_id"], "title": s["title"], "why": s["why"], "relative": s.get("relative"),
                 "thumbnail_file": _save_thumb(s.get("thumbnail"), vdir / "thumbnail.jpg")}
        try:
            subs = ytdlp.subtitles(vurl, vdir / "subs")
            entry["subtitles"] = [str(p) for p in subs]
            text = ytdlp.vtt_to_text(subs[0]) if subs else None
            if text:
                (vdir / "transcript.txt").write_text(text, encoding="utf-8")
                entry["hook_text"] = " ".join(text.split()[:80])  # ≈ primeros 30 s: forma del gancho
                entry["transcript_words"] = len(text.split())
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
        "what_worked": patterns.what_worked(rows),
        "topics_ranked": patterns.topics_ranked(rows),
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


def _what_worked_md(a: dict) -> list[str]:
    ww = a.get("what_worked") or {}
    lines = ["## Qué le funcionó más (observación con datos)", f"Método: {ww.get('method')}.", ""]
    for fmt, g in (ww.get("by_format") or {}).items():
        lines.append(f"### Formato: {fmt} (n={g.get('n')})")
        if "note" in g:
            lines += [f"- {g['note']}", ""]
            continue
        lines += [f"- Top: " + "; ".join(f"«{v['title'][:60]}» ×{v['relative']}" for v in g["top_videos"][:5])]
        for f in g["rasgos_de_titulo"]:
            lines.append(f"- {f['rasgo']}: {int(f['top'] * 100)} % en top vs {int(f['resto'] * 100)} % en resto "
                         f"(evidencia {f['evidencia']})")
        for k, v in g["numericos"].items():
            lines.append(f"- {k}: mediana top {v['mediana_top']} vs resto {v['mediana_resto']}")
        if g.get("duracion_mas_comun_top"):
            lines.append(f"- Duración más común en el top: {g['duracion_mas_comun_top']}")
        lines.append("")
    if ww.get("title_formulas"):
        lines += ["Fórmulas de título recurrentes: " + ", ".join(f"«{p}» ({c})" for p, c in ww["title_formulas"][:8]), ""]
    if a.get("topics_ranked"):
        lines += ["Temas con mejor rendimiento relativo mediano: " +
                  ", ".join(f"{t['tema']} ({t['relative_mediano']}, n={t['videos']})" for t in a["topics_ranked"][:10]), ""]
    lines += ["## Por qué (hipótesis — completar tras revisar miniaturas, keyframes y transcripciones)",
              "| Hipótesis | Evidencia | Confianza |", "|---|---|---|", "| | | |", ""]
    return lines


def write_report(a: dict, path: Path) -> None:
    ch = a["channel"]
    vids = [v for v in a["videos"] if v.get("relative") is not None]
    top = sorted(vids, key=lambda v: -v["relative"])[:10]
    lines = [
        f"# Análisis de referencia: {ch.get('title')}",
        "",
        f"Consultado: {a['retrieved_at']} · Fuente: {ch.get('source', 'youtube_data_api_v3')} · Canal `{ch.get('channel_id')}`",
        "",
        "## Datos públicos (observación)",
        f"- Suscriptores: {ch.get('subscribers') if ch.get('subscribers') is not None else 'ocultos / no disponibles'}",
        f"- Visualizaciones totales: {ch.get('views') if ch.get('views') is not None else 'no disponible'}",
        f"- Videos: {ch.get('videos')} (analizados en catálogo: {len(a['videos'])})",
        "",
        "## Rendimiento relativo",
        f"Fórmula: {a['cohort_formula']}. Promedio histórico, no velocidad actual.",
        "",
        "| Video | Formato | Edad (d) | Visitas | Relativo |",
        "|---|---|---|---|---|",
        *[f"| {v['title'][:70]} | {v['format']} | {v.get('age_days')} | {v['views']} | {v['relative']} |" for v in top],
        "",
        "",
        *_what_worked_md(a),
        "## Limitaciones",
        *[f"- {x}" for x in a["access_limitations"]],
        "",
        "## Hallazgos (a completar con revisión visual de keyframes)",
        "Para cada conclusión: **Observación** (dato + evidencia) · **Hipótesis** · **Confianza** (baja/media/alta).",
        "No confundir correlación con causalidad. No atribuir el canal a herramientas de IA sin evidencia.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
