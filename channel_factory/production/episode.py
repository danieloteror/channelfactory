"""Producción de un episodio desde su manifiesto (episode.yaml).

Etapas cacheadas por contenido: narración por escena → render por escena → mezcla
→ ensamblado (master limpio, subtítulos, transcripción, manifiesto de escenas,
miniatura) → QA. Regenerar una escena solo invalida sus artefactos.

Estructura de un episodio:
    channels/<canal>/episodes/<episodio>/
      episode.yaml   claims.json   assets/   work/ (caché, fuera de Git)   render/
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import yaml

from ..models import EpisodeManifest, Scene
from ..orchestrator.artifacts import Artifacts, cache_key, sha256_file
from . import audio, subtitles
from .compositor import render_scene, scene_duration

COMPOSITOR_VERSION = "1"


class EpisodeWorkspace:
    def __init__(self, episode_dir: Path):
        self.dir = Path(episode_dir)
        self.manifest_path = self.dir / "episode.yaml"
        self.work = self.dir / "work"
        self.render_dir = self.dir / "render"

    @classmethod
    def for_episode(cls, settings, channel_id: str, episode_id: str) -> "EpisodeWorkspace":  # noqa: ANN001
        return cls(settings.channel_dir(channel_id) / "episodes" / episode_id)

    def load(self) -> EpisodeManifest:
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"falta el guion/manifiesto {self.manifest_path}")
        return EpisodeManifest.model_validate(yaml.safe_load(self.manifest_path.read_text(encoding="utf-8")))

    def save(self, m: EpisodeManifest) -> None:
        self.manifest_path.write_text(
            yaml.safe_dump(m.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def timeline(self, m: EpisodeManifest) -> list[dict]:
        t, out = 0.0, []
        for s in m.scenes:
            d = scene_duration(s)
            out.append({"scene_id": s.scene_id, "start": round(t, 3), "end": round(t + d, 3),
                        "narration_start": round(t + s.lead_in_s, 3), "beat": s.beat, "clip_hint": s.clip_hint,
                        "claims": s.claims, "vertical_roi": s.visual.vertical_roi, "text": s.text})
            t += d
        return out


# ---------------------------------------------------------------- narración


def narrate(ws: EpisodeWorkspace, tts, voice_id: str, *, speed: float = 1.0, artifacts: Artifacts | None = None,  # noqa: ANN001
            asr=None, qa_sample: int = 3, lexicon: dict | None = None, model_tag: str = "kokoro-v1.0",  # noqa: ANN001
            only: list[str] | None = None) -> dict:
    """Sintetiza cada escena (WAV por escena) y registra su duración REAL."""
    from ..quality.asr import roundtrip

    m = ws.load()
    lex_hash = hashlib.sha256(json.dumps(lexicon or {}, sort_keys=True).encode()).hexdigest()[:12]
    out_dir = ws.work / "tts"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"voice": voice_id, "scenes": [], "asr_checks": []}
    spoken = [s for s in m.scenes if s.text.strip()]
    for s in m.scenes:
        if not s.text.strip() or (only and s.scene_id not in only):
            continue
        key = cache_key("tts", {"text": s.text, "voice": voice_id, "speed": speed, "lexicon": lex_hash, "model": model_tag})
        hit = artifacts.lookup("tts", key) if artifacts else None
        path = Path(hit["path"]) if hit else out_dir / f"{s.scene_id}_{key[:10]}.wav"
        cached = hit is not None or (artifacts is None and path.exists())
        if not cached:
            res = tts.synthesize(s.text, voice_id, path, speed=speed) if not voice_id.startswith("blend:") else _blend(tts, s.text, voice_id, path, speed)
            if artifacts:
                artifacts.record("tts", key, path, kind="audio", episode_id=m.episode_id, inputs={"scene": s.scene_id})
            dur = res.duration_s
        else:
            import soundfile as sf

            dur = sf.info(str(path)).duration
        s.duration_s, s.audio_path, s.status = round(dur, 3), str(path.relative_to(ws.dir)), "audio_ready"
        report["scenes"].append({"scene_id": s.scene_id, "duration_s": s.duration_s, "cached": cached})
    if asr is not None and asr.available() and spoken:
        picks = {spoken[0].scene_id, spoken[len(spoken) // 2].scene_id, spoken[-1].scene_id}
        for s in spoken:
            if s.scene_id in picks and len(report["asr_checks"]) < qa_sample and s.audio_path:
                rt = roundtrip(s.text, asr.transcribe(ws.dir / s.audio_path))
                report["asr_checks"].append({"scene_id": s.scene_id, **asdict(rt)})
    ws.save(m)
    return report


def _blend(tts, text: str, voice_id: str, path: Path, speed: float):  # noqa: ANN001, ANN202
    import soundfile as sf

    from ..providers.base import SynthResult
    from ..providers.tts_kokoro import limit_peak, trim_silence
    from .voices import synth

    a, sr = synth(tts, text, voice_id, speed)
    a = limit_peak(trim_silence(a, sr))
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, a, sr, subtype="PCM_24")
    return SynthResult(path, len(a) / sr, sr, voice_id)


# ---------------------------------------------------------------- render


def _scene_key(ws: EpisodeWorkspace, s: Scene, w: int, h: int, fps: int, vertical: bool) -> str:
    assets = []
    for spec in [*(s.visual.characters or []), *(s.visual.props or [])]:
        for rel in [spec.get("image"), *[x.get("image") for x in spec.get("swaps", [])]]:
            if rel and (ws.dir / rel).exists():
                assets.append(sha256_file(ws.dir / rel))
    if s.visual.background and not s.visual.background.startswith("#") and (ws.dir / s.visual.background).exists():
        assets.append(sha256_file(ws.dir / s.visual.background))
    if s.visual.video and (ws.dir / s.visual.video["path"]).exists():
        assets.append(sha256_file(ws.dir / s.visual.video["path"]))
    return cache_key("scene_render", {"scene": s.model_dump(mode="json", exclude={"status", "cost_usd"}),
                                      "assets": assets, "size": [w, h, fps], "vertical": vertical,
                                      "v": COMPOSITOR_VERSION})


def _render_one(args: tuple) -> dict:
    scene_json, prev_json, ep_dir, out, width, height, fps, vertical = args
    s = Scene.model_validate(scene_json)
    prev = Scene.model_validate(prev_json) if prev_json else None
    rep = render_scene(s, Path(ep_dir), Path(out), width=width, height=height, fps=fps, prev_scene=prev,
                       vertical=vertical)
    return asdict(rep)


def render(ws: EpisodeWorkspace, *, width: int, height: int, fps: int, artifacts: Artifacts | None = None,
           vertical: bool = False, only: list[str] | None = None, workers: int = 1) -> list[dict]:
    """Renderiza las escenas pendientes (en paralelo con `workers` > 1). Las cacheadas no se repiten."""
    from concurrent.futures import ProcessPoolExecutor

    m = ws.load()
    out_dir = ws.work / ("scenes_v" if vertical else "scenes")
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    todo = []
    for i, s in enumerate(m.scenes):
        key = _scene_key(ws, s, width, height, fps, vertical)
        out = out_dir / f"{s.scene_id}_{key[:10]}.mp4"
        hit = artifacts.lookup("scene_render", key) if artifacts else (out if out.exists() else None)
        if hit is None and not (only and s.scene_id not in only):
            prev = m.scenes[i - 1].model_dump(mode="json") if i > 0 else None
            todo.append((s.scene_id, key, (s.model_dump(mode="json"), prev, str(ws.dir), str(out), width, height, fps,
                                           vertical)))
        else:
            results[s.scene_id] = {"scene_id": s.scene_id, "path": str(out), "cached": True}
    if workers > 1 and len(todo) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            reps = list(ex.map(_render_one, [t[2] for t in todo]))
    else:
        reps = [_render_one(t[2]) for t in todo]
    for (sid, key, args), rep in zip(todo, reps):
        if artifacts:
            artifacts.record("scene_render", key, args[3], kind="video", episode_id=m.episode_id,
                             inputs={"scene": sid, "vertical": vertical})
        results[sid] = {"scene_id": sid, "cached": False, **rep}
    return [results[s.scene_id] for s in m.scenes]


# ---------------------------------------------------------------- mezcla y ensamblado


def _resolve_media(ws: EpisodeWorkspace, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    for base in (ws.dir, ws.dir.parent.parent):  # episodio, luego canal
        if (base / rel).exists():
            return base / rel
    raise FileNotFoundError(rel)


def mix_audio(ws: EpisodeWorkspace, *, lufs: float, true_peak: float, music_db: float, ducking: bool) -> dict:
    from .compositor import resolve_at

    m = ws.load()
    tl = ws.timeline(m)
    total = tl[-1]["end"] if tl else 0.0
    segs, sfx = [], []
    for s, t in zip(m.scenes, tl):
        if s.audio_path:
            segs.append((t["narration_start"], ws.dir / s.audio_path))
        for fx in s.sfx:
            at = resolve_at(s, fx, s.duration_s or 0.0, 0.0) or 0.0
            sfx.append((t["start"] + at, _resolve_media(ws, fx["file"]), float(fx.get("gain_db", -12))))
    ws.render_dir.mkdir(parents=True, exist_ok=True)
    narr = audio.build_narration(segs, total, ws.work / "narration.wav")
    sfx_track = audio.build_sfx(sfx, total, ws.work / "sfx.wav")
    music = _resolve_media(ws, m.music["path"]) if m.music and m.music.get("path") else None
    res = audio.mix(narr, ws.work / "mix.wav", total_s=total, music=music,
                    music_db=float((m.music or {}).get("gain_db", music_db)), ducking=ducking, sfx=sfx_track,
                    lufs=lufs, true_peak=true_peak)
    res["music"] = m.music
    return res


def assemble(ws: EpisodeWorkspace, scene_videos: list[dict], *, name: str = "master_clean",
             burn_subtitles: bool = False) -> dict:
    m = ws.load()
    ws.render_dir.mkdir(parents=True, exist_ok=True)
    lst = ws.work / f"{name}_concat.txt"
    lst.write_text("".join(f"file '{Path(v['path']).resolve()}'\n" for v in scene_videos))
    video = ws.work / f"{name}_video.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    str(video)], check=True)
    out = ws.render_dir / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(ws.work / "mix.wav"),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-shortest", "-movflags", "+faststart", str(out)], check=True)
    tl = ws.timeline(m)
    cues = []
    for s, t in zip(m.scenes, tl):
        if s.text.strip() and s.duration_s:
            cues += subtitles.cues_for_scene(s.text, t["narration_start"], s.duration_s)
    subs = subtitles.write(cues, ws.render_dir / "subtitles_en")
    transcript = {"language": m.language, "script_version": m.script_version, "timing_precision": "segment",
                  "segments": [{"scene_id": t["scene_id"], "start": t["narration_start"],
                                "end": round(t["narration_start"] + (s.duration_s or 0), 3), "text": s.text}
                               for s, t in zip(m.scenes, tl) if s.text.strip()]}
    (ws.render_dir / "transcript.json").write_text(json.dumps(transcript, indent=2))
    (ws.render_dir / "scenes.json").write_text(json.dumps({"episode_id": m.episode_id, "script_version": m.script_version,
                                                           "scenes": tl}, indent=2))
    result = {"master": str(out), "subtitles": subs, "transcript": str(ws.render_dir / "transcript.json"),
              "scenes": str(ws.render_dir / "scenes.json"), "sha256": sha256_file(out)}
    if burn_subtitles:
        burned = ws.render_dir / f"{name}_subtitled.mp4"
        srt = subs["srt"].replace("\\", "/").replace(":", "\\:")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out), "-vf",
                        f"subtitles='{srt}':force_style='FontName=Inter,FontSize=20,Outline=2,MarginV=40'",
                        "-c:a", "copy", str(burned)], check=True)
        result["subtitled"] = str(burned)
    shutil.copy(ws.manifest_path, ws.render_dir / "episode.snapshot.yaml")
    return result


def thumbnail(ws: EpisodeWorkspace, scene_videos: list[dict], *, size=(1280, 720)) -> str | None:  # noqa: ANN001
    """Miniatura: fotograma de una escena (o imagen dada) + título corto con tipografía real."""
    from PIL import Image

    from .graphics import draw_text

    m = ws.load()
    spec = m.thumbnail or {}
    if spec.get("image"):
        img = Image.open(_resolve_media(ws, spec["image"])).convert("RGBA")
    else:
        sid = spec.get("scene_id") or (m.scenes[0].scene_id if m.scenes else None)
        match = next((v for v in scene_videos if v["scene_id"] == sid), None)
        if not match:
            return None
        frame = Path(match["path"]).with_suffix(".last.png")
        if not frame.exists():
            return None
        img = Image.open(frame).convert("RGBA")
    img = img.resize(size, Image.LANCZOS)
    if spec.get("title"):
        draw_text(img, {"text": spec["title"], "x": spec.get("x", 0.5), "y": spec.get("y", 0.8),
                        "size": spec.get("size", 120), "color": spec.get("color", "#FFFFFF"),
                        "stroke": spec.get("stroke", 10), "stroke_color": spec.get("stroke_color", "#000000"),
                        "family": spec.get("family", "Inter"), "style": spec.get("style", "Black"), "anim": "none",
                        "max_width": 0.9}, 1.0, size[1] / 1080)
    out = ws.render_dir / "thumbnail.jpg"
    img.convert("RGB").save(out, quality=90)
    return str(out)
