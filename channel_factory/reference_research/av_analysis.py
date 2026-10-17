"""Análisis audiovisual de un video local: cortes, keyframes, movimiento, paleta, voz y gancho.

Cada métrica registra su método. Herramientas opcionales (si están instaladas):
PySceneDetect (cortes), OpenCV (flujo óptico), librosa (onsets/tempo), Silero VAD (voz).
Sin ellas se usan alternativas con FFmpeg/numpy, marcadas como tales.
Estas señales preparan la revisión; no "entienden" la imagen: el estilo visual lo
juzga después un modelo con visión o una persona sobre los keyframes.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from ..quality.technical import ffprobe


def _frames_gray(path: Path, fps: float = 2.0, w: int = 160, h: int = 90) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo",
         "-pix_fmt", "gray", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w)


def _frames_rgb(path: Path, fps: float = 0.5, w: int = 96, h: int = 54) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w, 3)


def scene_cuts(path: Path, threshold: float = 0.3) -> tuple[list[float], str]:
    try:
        from scenedetect import ContentDetector, detect

        scenes = detect(str(path), ContentDetector())
        return [s[0].get_seconds() for s in scenes[1:]], "pyscenedetect.ContentDetector"
    except ImportError:
        log = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path), "-vf", f"select='gt(scene,{threshold})',showinfo", "-an",
             "-f", "null", "-"], capture_output=True, text=True).stderr
        return [float(x) for x in re.findall(r"pts_time:([\d.]+)", log)], f"ffmpeg scene>{threshold}"


def keyframes(path: Path, cuts: list[float], duration: float, out_dir: Path, max_frames: int = 40) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    bounds = [0.0, *cuts, duration]
    mids = [(a + b) / 2 for a, b in zip(bounds, bounds[1:])]
    if len(mids) > max_frames:
        idx = np.linspace(0, len(mids) - 1, max_frames).round().astype(int)
        mids = [mids[i] for i in idx]
    paths = []
    for i, t in enumerate(mids):
        p = out_dir / f"kf_{i:03d}_{t:07.2f}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=640:-2", str(p)], check=False)
        if p.exists():
            paths.append(str(p))
    return paths


def motion_profile(path: Path) -> dict:
    """Movimiento medio entre fotogramas (2 fps). Distingue imagen estática editada de animación continua."""
    g = _frames_gray(path).astype(np.float32)
    if len(g) < 2:
        return {"method": "none", "mean": None}
    try:
        import cv2

        mags = []
        for a, b in zip(g[:-1], g[1:]):
            flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            mags.append(float(np.linalg.norm(flow, axis=2).mean()))
        method = "opencv.farneback (px/frame a 160x90, 2 fps)"
    except ImportError:
        mags = [float(np.abs(b - a).mean()) for a, b in zip(g[:-1], g[1:])]
        method = "numpy abs-diff (nivel de gris medio, 160x90, 2 fps)"
    m = np.array(mags)
    still = float((m < (0.15 if "opencv" in method else 1.5)).mean())
    return {"method": method, "mean": round(float(m.mean()), 4), "p90": round(float(np.percentile(m, 90)), 4),
            "still_ratio": round(still, 3)}


def palette(path: Path, k: int = 6, iters: int = 12) -> list[dict]:
    px = _frames_rgb(path).reshape(-1, 3).astype(np.float32)
    if len(px) == 0:
        return []
    rng = np.random.default_rng(0)
    sample = px[rng.choice(len(px), size=min(len(px), 20000), replace=False)]
    cent = sample[rng.choice(len(sample), size=k, replace=False)]
    for _ in range(iters):
        lab = np.argmin(((sample[:, None] - cent[None]) ** 2).sum(-1), axis=1)
        cent = np.array([sample[lab == i].mean(0) if (lab == i).any() else cent[i] for i in range(k)])
    counts = np.bincount(lab, minlength=k) / len(lab)
    order = np.argsort(-counts)
    return [{"hex": "#%02x%02x%02x" % tuple(int(c) for c in cent[i]), "share": round(float(counts[i]), 3)} for i in order]


def _audio(path: Path, sr: int = 16000) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le",
                          "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def speech_activity(path: Path) -> dict:
    a = _audio(path)
    if a.size == 0:
        return {"method": "none", "speech_ratio": None}
    try:
        from ..quality.vad import SileroVAD

        segs = SileroVAD().segments(a)
        method = "silero-vad (ONNX, onnxruntime)"
    except (ImportError, FileNotFoundError):
        win = 400  # 25 ms
        n = a.size // win
        e = 10 * np.log10((a[: n * win].reshape(n, win) ** 2).mean(1) + 1e-10)
        thr = max(np.percentile(e, 30) + 6, -45)
        on = e > thr
        segs, start = [], None
        for i, v in enumerate(on):
            t = i * win / 16000
            if v and start is None:
                start = t
            if not v and start is not None:
                if t - start > 0.2:
                    segs.append((start, t))
                start = None
        method = "energía (aproximación; instala silero-vad para precisión)"
    dur = a.size / 16000
    speech = sum(e - s for s, e in segs)
    gaps = [b[0] - a_[1] for a_, b in zip(segs, segs[1:])]
    return {"method": method, "speech_ratio": round(speech / dur, 3) if dur else None,
            "first_speech_s": round(segs[0][0], 2) if segs else None,
            "pauses_over_0_7s": sum(1 for g in gaps if g > 0.7),
            "median_pause_s": round(float(np.median(gaps)), 3) if gaps else None}


def rhythm(path: Path) -> dict:
    """Pulsos y comienzos de eventos sonoros (librosa). Mapa orientativo, no estructura musical real."""
    try:
        import librosa
    except ImportError:
        return {"method": "no disponible (instala librosa)", "tempo_bpm": None, "onsets_per_min": None}
    sr = 22050
    y = _audio(path, sr)  # decodificamos con FFmpeg: librosa/soundfile no leen MP4
    if y.size == 0:
        return {"method": "librosa", "tempo_bpm": None, "onsets_per_min": None}
    tempo, _beats = librosa.beat.beat_track(y=y, sr=sr)
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    return {"method": "librosa", "tempo_bpm": round(float(np.atleast_1d(tempo)[0]), 1),
            "onsets_per_min": round(len(onsets) / (len(y) / sr) * 60, 1)}


def analyze_file(path: Path, out_dir: Path, transcript: str | None = None) -> dict:
    info = ffprobe(path)
    dur = float(info["format"].get("duration", 0))
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    cuts, cut_method = scene_cuts(path)
    rep = {
        "file": str(path), "duration_s": round(dur, 2),
        "resolution": f"{v['width']}x{v['height']}" if v else None,
        "cuts": {"method": cut_method, "count": len(cuts), "per_minute": round(len(cuts) / (dur / 60), 2) if dur else None,
                 "first_15s": sum(1 for c in cuts if c < 15), "times": [round(c, 2) for c in cuts[:500]]},
        "keyframes": keyframes(path, cuts, dur, out_dir / "keyframes"),
        "motion": motion_profile(path),
        "palette": palette(path),
        "speech": speech_activity(path),
        "rhythm": rhythm(path),
    }
    if transcript:
        words = len(re.findall(r"[A-Za-zÀ-ÿ0-9']+", transcript))
        speech_s = (rep["speech"].get("speech_ratio") or 0) * dur
        rep["text"] = {"words": words, "wpm_overall": round(words / (dur / 60), 1) if dur else None,
                       "wpm_speech": round(words / (speech_s / 60), 1) if speech_s else None}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "av_analysis.json").write_text(json.dumps(rep, indent=2))
    return rep
