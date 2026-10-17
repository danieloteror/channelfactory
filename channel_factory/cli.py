"""CLI estable de Channel Factory. Las órdenes naturales del agente se traducen a estos comandos."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

import typer

from .config import load_settings

app = typer.Typer(help="Channel Factory: producción de videos explicativos, clips y publicación.", no_args_is_help=True)
voices_app = typer.Typer(help="Audiciones y selección de voz.", no_args_is_help=True)
channel_app = typer.Typer(help="Perfiles de canal.", no_args_is_help=True)
episode_app = typer.Typer(help="Episodios individuales.", no_args_is_help=True)
month_app = typer.Typer(help="Operaciones de lote mensual.", no_args_is_help=True)
publish_app = typer.Typer(help="Metricool (opcional, desactivado por defecto): validación, bandeja y conciliación.",
                          no_args_is_help=True)
clips_app = typer.Typer(help="Contrato con Clip Master.", no_args_is_help=True)
models_app = typer.Typer(help="Modelos locales.", no_args_is_help=True)
providers_app = typer.Typer(help="Proveedores y herramientas propias.", no_args_is_help=True)
for name, sub in (("voices", voices_app), ("channel", channel_app), ("episode", episode_app), ("month", month_app),
                  ("publish", publish_app), ("clips", clips_app), ("models", models_app),
                  ("providers", providers_app)):
    app.add_typer(sub, name=name)


def _out(data) -> None:  # noqa: ANN001
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _blocked(detail: str) -> None:
    _out({"blocked": detail})
    raise typer.Exit(2)


def _ctx():  # noqa: ANN202
    from .db import open_db

    s = load_settings()
    return s, open_db(s)


def _queue(s, db):  # noqa: ANN001, ANN202
    from .orchestrator.jobs import JobQueue

    r = s.cfg.runtime
    return JobQueue(db, r.lease_seconds, r.retry_backoff_seconds, r.max_attempts)


def _lexicon(s, cid: str) -> dict:  # noqa: ANN001
    import yaml

    p = s.channel(cid).voice.lexicon
    return (yaml.safe_load(s.path(p).read_text()) or {}) if p and s.path(p).exists() else {}


# ---------------------------------------------------------------- diagnóstico


@app.command()
def doctor(smoke: bool = typer.Option(False, help="Renderiza un episodio sintético de prueba."),
           network: bool = typer.Option(True, help="Comprueba acceso a hosts externos.")) -> None:
    """Hardware, herramientas, modelos, red, credenciales (presencia) y bloqueos reales."""
    from . import doctor as doc

    s = load_settings()
    rep = doc.run(s, network=network)
    if smoke:
        from .production.smoke import run
        from .providers.tts_kokoro import KokoroTTS

        tts = KokoroTTS(s.models_dir)
        r = run(s.state_dir / "smoke", tts=tts if tts.health().usable else None)
        rep["smoke"] = {"tts": r["tts"], "qa_ok": r["qa"]["ok"], "qa_problems": r["qa"]["problems"],
                        "master": r["assemble"]["master"], "seconds": r["seconds"]}
    _out(rep)


@app.command()
def setup() -> None:
    """Prepara directorios de estado y precarga los modelos base (Kokoro + ASR)."""
    from .production.voices import preload
    from .quality.asr import ASR_URL

    s = load_settings()
    s.state_dir.mkdir(parents=True, exist_ok=True)
    _out({"state_dir": str(s.state_dir), "kokoro": preload(s.models_dir),
          "asr": "ejecuta `channel-factory models fetch-asr` (" + ASR_URL + ")"})


@models_app.command("fetch-asr")
def fetch_asr() -> None:
    """Descarga Whisper base.en (sherpa-onnx) para QA de narración y transcripción local."""
    import tarfile
    import urllib.request

    from .quality.asr import ASR_URL, WhisperASR

    s = load_settings()
    dest = s.models_dir / "asr"
    dest.mkdir(parents=True, exist_ok=True)
    if not WhisperASR(s.models_dir).available():
        tmp = dest / "whisper.tar.bz2"
        urllib.request.urlretrieve(ASR_URL, tmp)
        with tarfile.open(tmp) as t:
            t.extractall(dest, filter="data")
        tmp.unlink()
    _out({"available": WhisperASR(s.models_dir).available(), "dir": str(dest)})


@providers_app.command("list")
def providers_list() -> None:
    from .providers.registry import builtin_providers

    s = load_settings()
    reg = s.root / "providers" / "registry.yaml"
    import yaml

    _out({"builtin": {k: v.__dict__ for k, v in builtin_providers(s).items()},
          "registry": yaml.safe_load(reg.read_text()) if reg.exists() else None})


@providers_app.command("check")
def providers_check() -> None:
    """Valida providers/user_tools.yaml sin ejecutar las herramientas."""
    from .providers.registry import check_user_tool, load_user_tools

    s = load_settings()
    _out({t.id: check_user_tool(t, s.root).__dict__ for t in load_user_tools(s.root / "providers" / "user_tools.yaml")})


# ---------------------------------------------------------------- canales


@channel_app.command("init")
def channel_init(channel_id: str, name: str = typer.Option(...), niche: str = typer.Option(...),
                 local: bool = typer.Option(True, help="Registrar en config/factory.local.yaml (fuera de Git).")) -> None:
    """Crea channels/<id>/ desde la plantilla y lo registra."""
    import yaml

    if not re.fullmatch(r"[a-z0-9_]+", channel_id):
        raise typer.BadParameter("id: minúsculas, números y _")
    s = load_settings()
    d = s.root / "channels" / channel_id
    if (d / "channel.yaml").exists():
        raise typer.BadParameter(f"ya existe {d / 'channel.yaml'}")
    d.mkdir(parents=True, exist_ok=True)
    tpl = (s.root / "channels" / "_template" / "channel.yaml").read_text()
    tpl = tpl.replace("__CHANNEL_ID__", channel_id).replace("__CHANNEL_NAME__", name).replace("__NICHE__", niche)
    (d / "channel.yaml").write_text(tpl)
    for sub in ("episodes", "characters", "brand", "reference", "voices"):
        (d / sub).mkdir(exist_ok=True)
    (d / "backlog.yaml").write_text("# Temas candidatos: [{title, angle, pillar}]\ntopics: []\n")
    target = s.root / "config" / ("factory.local.yaml" if local else "factory.yaml")
    data = yaml.safe_load(target.read_text()) if target.exists() else {}
    data = data or {}
    data.setdefault("channels", {})[channel_id] = {"enabled": True, "profile": f"channels/{channel_id}/channel.yaml"}
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    from .config import _load

    _load.cache_clear()
    _out({"created": str(d), "registered_in": str(target)})


@channel_app.command("list")
def channel_list() -> None:
    s = load_settings()
    _out({cid: {"enabled": r.enabled, "profile": r.profile} for cid, r in s.cfg.channels.items()})


# ---------------------------------------------------------------- referencias


@app.command("analyze-channel")
def analyze_channel(url: str, channel: Optional[str] = typer.Option(None, "--channel", help="Guardar en el canal propio."),
                    catalog_limit: int = 500, deep: int = 10,
                    enrich: int = typer.Option(80, help="Videos con metadatos completos si no hay API key."),
                    download_media: bool = typer.Option(False, help="Solo si el uso está permitido.")) -> None:
    """Catálogo, métricas públicas, cohortes, qué le funcionó, muestra profunda y análisis audiovisual."""
    from .reference_research.analyze import analyze_channel as run

    s, db = _ctx()
    slug = re.sub(r"[^a-z0-9]+", "_", url.lower().split("?")[0].rstrip("/").split("@")[-1].split("/")[0])[:40]
    base = s.channel_dir(channel) / "reference" if channel else s.state_dir / "reference"
    out = base / slug
    from .reference_research.youtube_api import ApiBlocked
    from .reference_research.ytdlp import YtDlpBlocked

    try:
        res = run(url, out, catalog_limit=catalog_limit, deep_sample=deep, download_media=download_media,
                  enrich=enrich, db=db)
    except (ApiBlocked, YtDlpBlocked) as e:
        _blocked(str(e))
    _out({"out": str(out), "report": str(out / "reference_report.md"), "videos": len(res["videos"]),
          "what_worked_formats": list(res["what_worked"]["by_format"]), "limitations": res["access_limitations"]})


@app.command("analyze-video")
def analyze_video(src: str, channel: Optional[str] = typer.Option(None, "--channel"),
                  download_media: bool = False) -> None:
    from .reference_research.analyze import analyze_video as run

    s = load_settings()
    base = s.channel_dir(channel) / "reference" / "videos" if channel else s.state_dir / "analysis"
    name = Path(src).stem if Path(src).exists() else re.sub(r"\W+", "_", src)[-24:]
    _out(run(src, base / name, download_media=download_media))


# ---------------------------------------------------------------- voces


@voices_app.command("preload")
def voices_preload(quantized: bool = False) -> None:
    """Descarga solo el modelo necesario (caché persistente) y verifica checksums."""
    from .production.voices import preload

    _out(preload(load_settings().models_dir, quantized))


@voices_app.command("list")
def voices_list() -> None:
    from .providers.tts_kokoro import KokoroTTS

    tts = KokoroTTS(load_settings().models_dir)
    h = tts.health()
    if not h.usable:
        _blocked(h.detail)
    _out([v.__dict__ for v in tts.voices()])


@voices_app.command("audition")
def voices_audition(channel: str = typer.Option(..., "--channel"),
                    voices: Optional[str] = typer.Option(None, help="Lista separada por comas (admite blend:...).")) -> None:
    """5–8 muestras de 20–30 s, verificación ASR, galería HTML y recomendación provisional."""
    from .production.voices import DEFAULT_CANDIDATES, audition
    from .providers.tts_kokoro import KokoroTTS
    from .quality.asr import WhisperASR

    s = load_settings()
    prof = s.channel(channel)
    tts = KokoroTTS(s.models_dir, lexicon=_lexicon(s, channel))
    if not tts.health().usable:
        _blocked(tts.health().detail)
    cands = ([(v, v) for v in voices.split(",")] if voices else
             [(v, v) for v in prof.voice.audition_candidates] or DEFAULT_CANDIDATES)
    out_dir = s.channel_dir(channel) / "voices" / "auditions"
    res = audition(tts, out_dir, cands, text=prof.voice.audition_text, speed=prof.voice.speed,
                   asr=WhisperASR(s.models_dir))
    if prof.voice.format_samples:
        from .production.voices import format_samples

        best = res[0].voice_id
        format_samples(tts, {k: (best, t) for k, t in prof.voice.format_samples.items()}, out_dir,
                       asr=WhisperASR(s.models_dir))
    _out({"gallery": str(out_dir / "index.html"), "ranking": [(r.voice_id, r.score, r.asr_wer, r.wpm) for r in res]})


@voices_app.command("select")
def voices_select(voice_id: str, channel: str = typer.Option(..., "--channel"),
                  provisional: bool = typer.Option(False, help="Fijar como provisional (piloto).")) -> None:
    from .production.voices import set_voice

    s = load_settings()
    set_voice(s.path(s.cfg.channels[channel].profile), voice_id, provisional)
    _out({"channel": channel, ("provisional_voice" if provisional else "selected_voice"): voice_id})


# ---------------------------------------------------------------- planificación y producción


@app.command("plan-month")
def plan_month(month: str, channel: str = typer.Option(..., "--channel"),
               long_videos: Optional[int] = typer.Option(None, "--long-videos")) -> None:
    """Crea o recupera el lote del mes (idempotente) con slots y temas del backlog."""
    from .editorial.planner import plan_month as plan

    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise typer.BadParameter("formato YYYY-MM")
    s, db = _ctx()
    prof = s.channel(channel)
    if not s.cfg.channels[channel].enabled:
        raise typer.BadParameter(f"el canal {channel} no está activo")
    res = plan(db, channel_id=channel, month=month, tz=prof.publishing.timezone,
               long_videos=long_videos or s.monthly(channel).long_videos, channel_dir=s.channel_dir(channel),
               published_file=s.path(prof.published_topics_file), banned=prof.banned_topics)
    _out(res)


@app.command("produce-month")
def produce_month(month: str, channel: str = typer.Option(..., "--channel")) -> None:
    """Encola la cadena de etapas de cada episodio del lote. No cambia el modo de publicación."""
    from .pipeline import enqueue_episode

    s, db = _ctx()
    b = db.one("SELECT * FROM batches WHERE channel_id=? AND month=?", (channel, month))
    if not b:
        raise typer.BadParameter("no hay lote: ejecuta plan-month primero")
    q = _queue(s, db)
    eps = db.query("SELECT episode_id, script_version FROM episodes WHERE batch_id=? ORDER BY slot_at", (b["batch_id"],))
    jobs = {e["episode_id"]: enqueue_episode(q, channel_id=channel, episode_id=e["episode_id"],
                                             script_version=e["script_version"]) for e in eps}
    _out({"batch_id": b["batch_id"], "mode": s.effective_publish_mode(channel), "episodes": len(eps),
          "jobs": sum(len(v) for v in jobs.values()), "next": "channel-factory worker"})


@episode_app.command("new")
def episode_new(episode_id: str, channel: str = typer.Option(..., "--channel"), title: str = typer.Option(...)) -> None:
    """Crea el esqueleto de un episodio (episode.yaml + claims.json + assets/)."""
    s = load_settings()
    d = s.channel_dir(channel) / "episodes" / episode_id
    if (d / "episode.yaml").exists():
        raise typer.BadParameter("el episodio ya existe")
    (d / "assets").mkdir(parents=True, exist_ok=True)
    tpl = (Path(__file__).parent / "templates" / "episode.yaml").read_text()
    (d / "episode.yaml").write_text(tpl.replace("__CHANNEL_ID__", channel).replace("__EPISODE_ID__", episode_id)
                                    .replace("__TITLE__", json.dumps(title)))
    (d / "claims.json").write_text(json.dumps({"claims": []}, indent=2))
    _out({"created": str(d)})


@episode_app.command("check")
def episode_check(episode_id: str, channel: str = typer.Option(..., "--channel")) -> None:
    """Valida manifiesto, assets y claims sin renderizar."""
    from .editorial import claims as cl
    from .production.episode import EpisodeWorkspace

    s = load_settings()
    ws = EpisodeWorkspace.for_episode(s, channel, episode_id)
    m = ws.load()
    problems = []
    for sc in m.scenes:
        for spec in [*sc.visual.characters, *sc.visual.props]:
            for rel in [spec.get("image"), *[x.get("image") for x in spec.get("swaps", [])]]:
                if rel and not (ws.dir / rel).exists():
                    problems.append(f"{sc.scene_id}: falta {rel}")
        bg = sc.visual.background
        if bg and not bg.startswith("#") and not (ws.dir / bg).exists():
            problems.append(f"{sc.scene_id}: falta fondo {bg}")
        text = sc.text.lower()
        anchors = [o.get("at_text") for o in sc.visual.overlays] + [o.get("until_text") for o in sc.visual.overlays]
        for spec in [*sc.visual.characters, *sc.visual.props]:
            anchors += [spec.get("enter", {}).get("at_text"), (spec.get("exit") or {}).get("at_text")]
            anchors += [x.get("at_text") for x in spec.get("swaps", [])]
        anchors += [x.get("at_text") for x in sc.sfx]
        for a in filter(None, anchors):
            if a.lower() not in text:
                problems.append(f"{sc.scene_id}: at_text '{a}' no aparece en la narración")
    cp = ws.dir / (m.claims_file or "claims.json")
    problems += cl.check(cl.load(cp), m) if cp.exists() else [f"falta {cp}"]
    words = sum(len(sc.text.split()) for sc in m.scenes)
    _out({"scenes": len(m.scenes), "words": words, "estimated_minutes_at_150wpm": round(words / 150, 1),
          "problems": problems, "ok": not problems})


@episode_app.command("render")
def episode_render(episode_id: str, channel: str = typer.Option(..., "--channel"),
                   vertical_preview: bool = typer.Option(False, help="Renderiza además las escenas en 9:16.")) -> None:
    """Narración → render → mezcla → master, subtítulos, miniatura → QA técnica."""
    from .orchestrator.artifacts import Artifacts
    from .pipeline import set_episode_state
    from .production.episode import EpisodeWorkspace, assemble, mix_audio, narrate, render, thumbnail
    from .providers.tts_kokoro import KokoroTTS
    from .quality.asr import WhisperASR
    from .quality.technical import check_video

    s, db = _ctx()
    prof = s.channel(channel)
    voice = prof.voice.effective_voice
    if not voice:
        raise typer.BadParameter("sin voz: voices audition → voices select")
    ws = EpisodeWorkspace.for_episode(s, channel, episode_id)
    art = Artifacts(db)
    tts = KokoroTTS(s.models_dir, lexicon=_lexicon(s, channel))
    lv, au = s.cfg.defaults.long_video, s.cfg.defaults.audio
    rep = {"narration": narrate(ws, tts, voice, speed=prof.voice.speed, artifacts=art, asr=WhisperASR(s.models_dir),
                                lexicon=_lexicon(s, channel))}
    scenes = render(ws, width=lv.width, height=lv.height, fps=lv.fps, artifacts=art,
                    workers=s.cfg.runtime.render_workers)
    rep["mix"] = mix_audio(ws, lufs=au.integrated_lufs, true_peak=au.true_peak_dbtp, music_db=au.music_bed_db,
                           ducking=au.ducking)
    rep["assemble"] = assemble(ws, scenes)
    rep["thumbnail"] = thumbnail(ws, scenes)
    qa = check_video(Path(rep["assemble"]["master"]), width=lv.width, height=lv.height, fps=lv.fps,
                     lufs=au.integrated_lufs, true_peak_max=au.true_peak_dbtp)
    rep["qa"] = qa.to_dict()
    (ws.render_dir / "qa_technical.json").write_text(json.dumps(rep["qa"], indent=2))
    if vertical_preview:
        rep["vertical"] = render(ws, width=s.cfg.defaults.clips.width, height=s.cfg.defaults.clips.height,
                                 fps=lv.fps, artifacts=art, vertical=True, workers=s.cfg.runtime.render_workers)
    if db.one("SELECT 1 FROM episodes WHERE episode_id=?", (episode_id,)):
        from .models import EpisodeState

        set_episode_state(db, episode_id, EpisodeState.qa if qa.ok else EpisodeState.failed)
    _out(rep)


@episode_app.command("clips")
def episode_clips(episode_id: str, channel: str = typer.Option(..., "--channel")) -> None:
    """Encarga los clips a Clip Master (perfil original_explainer) y valida su respuesta."""
    from .clip_master_adapter.adapter import build_request, run_and_validate
    from .clip_master_adapter.transport import get_transport
    from .production.episode import EpisodeWorkspace

    s = load_settings()
    ws = EpisodeWorkspace.for_episode(s, channel, episode_id)
    req = build_request(ws.render_dir, channel_id=channel, episode_id=episode_id, job_id=f"{episode_id}-clips-v1",
                        clips_cfg=s.cfg.defaults.clips)
    _out(run_and_validate(get_transport(s.cfg.clip_master), req, ws.work / "clips"))


# ---------------------------------------------------------------- trabajos


@app.command()
def worker(once: bool = False, idle_sleep: float = 5.0) -> None:
    """Worker persistente: procesa la cola sin depender de una sesión de chat."""
    import logging

    from . import pipeline  # registra handlers
    from .orchestrator.worker import loop

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s, db = _ctx()
    loop(_queue(s, db), pipeline.services(s, db), idle_sleep=idle_sleep, once=once,
         heartbeat_s=s.cfg.runtime.heartbeat_seconds)


@app.command()
def status(channel: Optional[str] = typer.Option(None, "--channel")) -> None:
    s, db = _ctx()
    where, params = ("WHERE channel_id=?", (channel,)) if channel else ("", ())
    _out({
        "batches": [dict(r) for r in db.query(f"SELECT batch_id, channel_id, month, status FROM batches {where}", params)],
        "episodes": [dict(r) for r in db.query(f"SELECT episode_id, title, state, slot_at FROM episodes {where} "
                                               "ORDER BY slot_at", params)],
        "jobs": [dict(r) for r in db.query(f"SELECT state, COUNT(*) n FROM jobs {where} GROUP BY state", params)],
        "blocked": [dict(r) for r in db.query(f"SELECT job_id, kind, episode_id, last_error FROM jobs "
                                              f"{where + (' AND' if where else 'WHERE')} state IN ('blocked','failed')",
                                              params)],
        "publications": [dict(r) for r in db.query(f"SELECT pub_id, network, post_type, state, scheduled_at FROM "
                                                   f"publications {where}", params)],
    })


@app.command()
def resume(target: str) -> None:
    """Reanuda un lote (reencola trabajos bloqueados/fallidos) o un trabajo concreto."""
    s, db = _ctx()
    q = _queue(s, db)
    if target.startswith("batch_"):
        rows = db.query("SELECT job_id FROM jobs WHERE state IN ('failed','blocked') AND episode_id IN "
                        "(SELECT episode_id FROM episodes WHERE batch_id=?)", (target,))
        for r in rows:
            q.retry(r["job_id"])
        _out({"requeued": len(rows)})
    else:
        q.retry(target)
        _out({"requeued": 1})


@app.command()
def retry(job_id: str, stage: Optional[str] = typer.Option(None, "--stage")) -> None:
    """Reintenta un trabajo (o la etapa indicada del mismo episodio)."""
    s, db = _ctx()
    q = _queue(s, db)
    if stage:
        j = q.get(job_id)
        row = db.one("SELECT job_id FROM jobs WHERE episode_id=? AND kind=?", (j.episode_id, stage))
        if not row:
            raise typer.BadParameter(f"no hay etapa {stage} para {j.episode_id}")
        job_id = row["job_id"]
    q.retry(job_id)
    _out({"requeued": job_id})


@app.command()
def budget() -> None:
    from .orchestrator.budget import Budget, caps_from_settings

    s, db = _ctx()
    _out({"caps": caps_from_settings(s).__dict__, "ledger": Budget(db, caps_from_settings(s)).report()})


# ---------------------------------------------------------------- publicación


@month_app.command("schedule")
def month_schedule(month: str, channel: str = typer.Option(..., "--channel")) -> None:
    """Prepara las publicaciones del lote (largos y clips válidos) en el ledger y las envía según el modo."""
    from .publishing.cli_ops import schedule_month

    s, db = _ctx()
    _out(schedule_month(s, db, channel, month))


@publish_app.command("capabilities")
def publish_capabilities() -> None:
    from .publishing.capabilities import http_api, mcp_handoff

    _out({"mcp_handoff": mcp_handoff().to_dict(), "http_api": http_api().to_dict()})


@publish_app.command("brands-refresh")
def brands_refresh(from_json: Path = typer.Option(..., "--from-json", exists=True)) -> None:
    """Guarda la instantánea de getBrandSettings obtenida por la sesión (MCP)."""
    s = load_settings()
    dest = s.state_dir / "metricool_brands.json"
    data = json.loads(from_json.read_text())
    dest.write_text(json.dumps(data, indent=2))
    brands = data.get("data", data) if isinstance(data, dict) else data
    _out({"saved": str(dest), "brands": [{"id": b.get("id"), "label": b.get("label"),
                                          "networks": list((b.get("networksData") or {}).keys())} for b in brands]})


@publish_app.command("outbox")
def publish_outbox() -> None:
    """Solicitudes pendientes para ejecutar con el MCP de Metricool en la sesión."""
    s = load_settings()
    d = s.state_dir / "outbox" / "metricool"
    _out([json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else [])


@publish_app.command("record-result")
def publish_record_result(pub_id: str, response_json: Path = typer.Argument(..., exists=True)) -> None:
    from .publishing.ledger import Ledger
    from .publishing.publisher import record_result

    s, db = _ctx()
    state = record_result(Ledger(db), pub_id, json.loads(response_json.read_text()))
    out = s.state_dir / "outbox" / "metricool" / f"{pub_id}.json"
    if out.exists():
        done = out.parent / "done"
        done.mkdir(exist_ok=True)
        shutil.move(str(out), done / out.name)
    _out({"pub_id": pub_id, "state": state})


@publish_app.command("reconcile")
def publish_reconcile(channel: str = typer.Option(..., "--channel"),
                      remote_json: Path = typer.Option(..., "--remote-json", exists=True)) -> None:
    """Concilia `unconfirmed`/`handoff`/`scheduled` con la salida de getScheduledPosts."""
    from .publishing.ledger import Ledger

    s, db = _ctx()
    led = Ledger(db)
    data = json.loads(remote_json.read_text())
    posts = data.get("data", data) if isinstance(data, dict) else data
    res = {p.pub_id: led.reconcile(p.pub_id, posts)
           for p in led.list(channel, ["unconfirmed", "handoff", "scheduled"])}
    _out(res)


@publish_app.command("confirm")
def publish_confirm(pub_id: str, final_url: str) -> None:
    """Marca como publicada SOLO con la URL final verificada en la red."""
    from .publishing.ledger import Ledger

    s, db = _ctx()
    Ledger(db).confirm_published(pub_id, final_url)
    _out({"pub_id": pub_id, "state": "published"})


@publish_app.command("status")
def publish_status(channel: Optional[str] = typer.Option(None, "--channel")) -> None:
    from .publishing.ledger import Ledger

    s, db = _ctx()
    _out([{k: p.row[k] for k in ("pub_id", "network", "post_type", "state", "scheduled_at", "remote_id",
                                 "last_error", "attempts")} for p in Ledger(db).list(channel)])


# ---------------------------------------------------------------- clips


@clips_app.command("contract-schema")
def clips_schema(out_dir: Path = Path("docs/schemas")) -> None:
    from .clip_master_adapter.contract import export_schemas

    _out([str(p) for p in export_schemas(out_dir)])


@clips_app.command("validate-result")
def clips_validate(request_json: Path, result_json: Path) -> None:
    from .clip_master_adapter.contract import validate_result
    from .models import ClipRequest, ClipResult
    from .quality.technical import ffprobe

    req = ClipRequest.model_validate_json(request_json.read_text())
    res = ClipResult.model_validate_json(result_json.read_text())
    chk = validate_result(req, res, source_duration_s=float(ffprobe(Path(req.source_video))["format"]["duration"]))
    _out(chk.__dict__)


if __name__ == "__main__":
    app()
