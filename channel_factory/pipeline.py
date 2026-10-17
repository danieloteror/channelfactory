"""Etapas del episodio como trabajos durables y su encadenamiento por lote.

Cada etapa es idempotente (clave `episodio:etapa:v<guion>`) y usa la caché de
artefactos, así que repetir `produce-month` o reiniciar el worker no repite
trabajo hecho ni gasto. Faltas de acceso → `Blocked` (no éxito simulado).
"""

from __future__ import annotations

import json
from pathlib import Path

from .db import DB, utcnow_iso
from .models import EpisodeState, ErrorClass
from .orchestrator.artifacts import Artifacts
from .orchestrator.jobs import JobQueue
from .orchestrator.worker import Blocked, StageContext, StageError, stage

CHAIN = ["episode.script", "episode.narration", "episode.render", "episode.assemble", "episode.qa", "episode.clips",
         "publish.prepare"]


def set_episode_state(db: DB, episode_id: str, state: EpisodeState, note: str | None = None) -> None:
    db.execute("UPDATE episodes SET state=?, updated_at=? WHERE episode_id=?", (state.value, utcnow_iso(), episode_id))
    db.event("episode", episode_id, f"state:{state.value}", {"note": note} if note else None)


def _block(db: DB, episode_id: str, msg: str) -> Blocked:
    set_episode_state(db, episode_id, EpisodeState.blocked, note=msg)
    return Blocked(msg)


def enqueue_episode(q: JobQueue, *, channel_id: str, episode_id: str, script_version: int = 1,
                    stages: list[str] | None = None) -> list[str]:
    ids, prev = [], None
    for kind in stages or CHAIN:
        job = q.enqueue(kind, {"channel_id": channel_id, "episode_id": episode_id},
                        f"{episode_id}:{kind}:v{script_version}", channel_id=channel_id, episode_id=episode_id,
                        depends_on=[prev] if prev else [])
        ids.append(job.job_id)
        prev = job.job_id
    return ids


# ---------------------------------------------------------------- handlers


def _ctx(ctx: StageContext):  # noqa: ANN202
    s = ctx.services["settings"]
    p = ctx.job.payload
    from .production.episode import EpisodeWorkspace

    return s, ctx.services["db"], p["channel_id"], p["episode_id"], EpisodeWorkspace.for_episode(s, p["channel_id"], p["episode_id"])


@stage("episode.script")
def h_script(ctx: StageContext) -> dict:
    from .editorial import claims as claims_mod

    s, db, cid, eid, ws = _ctx(ctx)
    if not ws.manifest_path.exists():
        raise _block(db, eid, f"falta el guion {ws.manifest_path}: escríbelo en sesión (agente) o configura un LLM con presupuesto")
    m = ws.load()
    claims_path = ws.dir / (m.claims_file or "claims.json")
    if not claims_path.exists():
        raise _block(db, eid, f"falta {claims_path}: cada afirmación verificable necesita fuente")
    problems = claims_mod.check(claims_mod.load(claims_path), m)
    if problems:
        raise _block(db, eid, "claims sin resolver: " + "; ".join(problems[:10]))
    set_episode_state(db, eid, EpisodeState.script_ready)
    return {"scenes": len(m.scenes), "script_version": m.script_version}


@stage("episode.narration")
def h_narration(ctx: StageContext) -> dict:
    from .production.episode import narrate
    from .providers.tts_kokoro import KokoroTTS
    from .quality.asr import WhisperASR

    s, db, cid, eid, ws = _ctx(ctx)
    prof = s.channel(cid)
    voice = prof.voice.effective_voice
    if not voice:
        raise _block(db, eid, f"{cid}: sin voz seleccionada (voices audition → voices select)")
    lex = {}
    if prof.voice.lexicon and s.path(prof.voice.lexicon).exists():
        import yaml

        lex = yaml.safe_load(s.path(prof.voice.lexicon).read_text()) or {}
    tts = ctx.services.get("tts") or KokoroTTS(s.models_dir, lexicon=lex)
    h = tts.health() if hasattr(tts, "health") else None
    if h is not None and not h.usable:
        raise _block(db, eid, f"TTS no disponible: {h.detail}")
    asr = ctx.services.get("asr") or WhisperASR(s.models_dir)
    rep = narrate(ws, tts, voice, speed=prof.voice.speed, artifacts=Artifacts(db), asr=asr, lexicon=lex)
    bad = [c for c in rep["asr_checks"] if not c["ok"]]
    if bad:
        raise StageError(f"narración con posibles omisiones/alucinaciones: {json.dumps(bad)[:800]}")
    set_episode_state(db, eid, EpisodeState.audio_ready)
    return rep


@stage("episode.render")
def h_render(ctx: StageContext) -> dict:
    from .production.episode import render

    s, db, cid, eid, ws = _ctx(ctx)
    lv = s.cfg.defaults.long_video
    set_episode_state(db, eid, EpisodeState.rendering)
    res = render(ws, width=lv.width, height=lv.height, fps=lv.fps, artifacts=Artifacts(db),
                 workers=s.cfg.runtime.render_workers)
    return {"scenes": len(res), "cached": sum(1 for r in res if r["cached"])}


@stage("episode.assemble")
def h_assemble(ctx: StageContext) -> dict:
    from .production.episode import assemble, mix_audio, render, thumbnail

    s, db, cid, eid, ws = _ctx(ctx)
    lv, au = s.cfg.defaults.long_video, s.cfg.defaults.audio
    scenes = render(ws, width=lv.width, height=lv.height, fps=lv.fps, artifacts=Artifacts(db))  # caché: no re-renderiza
    mix = mix_audio(ws, lufs=au.integrated_lufs, true_peak=au.true_peak_dbtp, music_db=au.music_bed_db, ducking=au.ducking)
    out = assemble(ws, scenes)
    out["thumbnail"] = thumbnail(ws, scenes)
    out["mix"] = mix
    return out


@stage("episode.qa")
def h_qa(ctx: StageContext) -> dict:
    from .quality.technical import check_video

    s, db, cid, eid, ws = _ctx(ctx)
    lv, au = s.cfg.defaults.long_video, s.cfg.defaults.audio
    set_episode_state(db, eid, EpisodeState.qa)
    rep = check_video(ws.render_dir / "master_clean.mp4", width=lv.width, height=lv.height, fps=lv.fps,
                      min_s=lv.range_seconds[0], max_s=lv.range_seconds[1], lufs=au.integrated_lufs,
                      true_peak_max=au.true_peak_dbtp)
    (ws.render_dir / "qa_technical.json").write_text(json.dumps(rep.to_dict(), indent=2))
    if not rep.ok:
        set_episode_state(db, eid, EpisodeState.failed, note="; ".join(rep.problems)[:500])
        raise StageError("QA técnica: " + "; ".join(rep.problems), ErrorClass.permanent)
    return rep.to_dict()


@stage("episode.clips")
def h_clips(ctx: StageContext) -> dict:
    from .clip_master_adapter.adapter import build_request, run_and_validate
    from .clip_master_adapter.transport import get_transport

    s, db, cid, eid, ws = _ctx(ctx)
    set_episode_state(db, eid, EpisodeState.clipping)
    try:
        transport = get_transport(s.cfg.clip_master)
    except Blocked as e:
        raise _block(db, eid, str(e)) from e
    req = build_request(ws.render_dir, channel_id=cid, episode_id=eid, job_id=f"{eid}-clips-v1",
                        clips_cfg=s.cfg.defaults.clips)
    rep = run_and_validate(transport, req, ws.work / "clips")
    if not rep["accepted"]:
        raise StageError(f"ningún clip válido: {rep['rejected'] or rep['problems']}")
    return rep


@stage("publish.prepare")
def h_publish_prepare(ctx: StageContext) -> dict:
    s, db, cid, eid, ws = _ctx(ctx)
    if not s.publishing_enabled:
        set_episode_state(db, eid, EpisodeState.ready_to_schedule,
                          note="publicación desactivada: listo para subir a mano (ver docs/metricool.md)")
        return {"publishing": "disabled", "render_dir": str(ws.render_dir)}
    mode = s.effective_publish_mode(cid)
    set_episode_state(db, eid, EpisodeState.ready_to_schedule,
                      note=f"modo {mode}" + (": no se envía nada" if mode == "preview" else ""))
    return {"mode": mode}


def services(settings, db: DB | None = None) -> dict:  # noqa: ANN001
    from .db import open_db

    return {"settings": settings, "db": db or open_db(settings)}


def batch_status(db: DB, batch_id: str) -> dict:
    eps = db.query("SELECT episode_id, title, state, slot_at FROM episodes WHERE batch_id=? ORDER BY slot_at", (batch_id,))
    jobs = db.query("SELECT kind, state, COUNT(*) n FROM jobs WHERE episode_id IN "
                    "(SELECT episode_id FROM episodes WHERE batch_id=?) GROUP BY kind, state", (batch_id,))
    return {"episodes": [dict(e) for e in eps], "jobs": [dict(j) for j in jobs]}


def episode_dir_exists(settings, channel_id: str, episode_id: str) -> bool:  # noqa: ANN001
    return (Path(settings.channel_dir(channel_id)) / "episodes" / episode_id / "episode.yaml").exists()
