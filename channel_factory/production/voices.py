"""Sistema de audiciones de voz: precarga, muestras, verificación ASR, galería y selección fija.

No clona voces de terceros. Las mezclas (`blend:a*0.6+b*0.4`) combinan estilos de
voces de Kokoro para obtener una voz propia. La recomendación automática solo usa
métricas medidas (inteligibilidad por ASR, ritmo, picos); la elección final requiere
escuchar las muestras.
"""

from __future__ import annotations

import hashlib
import html
import json
import platform
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import yaml

from ..providers.tts_kokoro import _PAUSE, MODEL_FILES, RELEASE_URL, KokoroTTS, limit_peak, normalize, trim_silence

# Texto genérico: términos difíciles, cantidades, siglas, una pregunta y una frase con humor.
AUDITION_TEXT = (
    "Here's a question that sounds simple: why does the same thing cost more every year? "
    "Imagine a coffee that cost 2.50 five years ago and costs 3.10 today. That's a 24% jump. [pause 0.3] "
    "Economists track changes like this with the CPI, the Consumer Price Index, which follows thousands of prices "
    "at once. So, is your coffee getting fancier? [pause 0.25] Sadly, no. Same coffee. Smaller victory."
)

DEFAULT_CANDIDATES: list[tuple[str, str]] = [
    ("am_michael", "Masculina americana, cálida, registro medio"),
    ("am_fenrir", "Masculina americana, más grave y firme"),
    ("am_puck", "Masculina americana, ligera y juguetona"),
    ("am_eric", "Masculina americana, neutra informativa"),
    ("af_heart", "Femenina americana, cálida y natural"),
    ("af_bella", "Femenina americana, enérgica"),
    ("bm_george", "Masculina británica, tono documental"),
    ("blend:am_michael*0.6+am_fenrir*0.4", "Mezcla propia: michael + fenrir"),
]

WPM_RANGE = (135.0, 170.0)


@dataclass
class AuditionResult:
    voice_id: str
    label: str
    file: str
    duration_s: float
    synth_s: float
    rtf: float
    words: int
    wpm: float
    peak_dbfs: float
    asr_wer: float | None = None
    asr_missing: list[str] | None = None
    asr_text: str | None = None
    score: float | None = None


def hardware_report() -> dict:
    import os

    du = shutil.disk_usage(Path.home())
    mem = None
    try:
        with open("/proc/meminfo") as fh:
            mem = int(fh.readline().split()[1]) // 1024
    except OSError:
        pass
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        accel = "apple_silicon"
    elif shutil.which("nvidia-smi"):
        accel = "cuda"
    else:
        accel = "cpu"
    return {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "ram_mb": mem,
        "accelerator": accel,
        "disk_free_gb": round(du.free / 1e9, 1),
        "recommended_backend": "mlx-audio (pendiente de adaptador)" if accel == "apple_silicon" else "kokoro-onnx (CPU)",
    }


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        while b := fh.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def preload(models_dir: Path, quantized: bool = False) -> dict:
    """Descarga solo los archivos necesarios a un directorio persistente y verifica checksums."""
    target = Path(models_dir) / "kokoro"
    target.mkdir(parents=True, exist_ok=True)
    names = ["kokoro-v1.0.int8.onnx" if quantized else "kokoro-v1.0.onnx", "voices-v1.0.bin"]
    report = {}
    for name in names:
        p = target / name
        if not p.exists():
            tmp = p.with_suffix(p.suffix + ".part")
            urllib.request.urlretrieve(RELEASE_URL.format(name=name), tmp)
            tmp.rename(p)
        ok = _sha256(p) == MODEL_FILES[name]
        report[name] = {"path": str(p), "bytes": p.stat().st_size, "sha256_ok": ok}
        if not ok:
            raise RuntimeError(f"checksum incorrecto para {name}; borra el archivo y repite")
    manifest = {
        "model": "Kokoro-82M (ONNX, thewh1teagle/kokoro-onnx model-files-v1.0)",
        "license": "Apache-2.0 (pesos hexgrad/Kokoro-82M)",
        "source": RELEASE_URL.format(name="<file>"),
        "files": report,
    }
    (target / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def resolve_style(tts: KokoroTTS, voice_id: str):  # noqa: ANN201
    """Id de voz tal cual, o vector de estilo mezclado para `blend:a*w+b*w`."""
    if not voice_id.startswith("blend:"):
        return voice_id
    eng = tts._engine()
    acc = None
    for term in voice_id[len("blend:"):].split("+"):
        name, w = term.split("*")
        style = eng.get_voice_style(name) * float(w)
        acc = style if acc is None else acc + style
    return acc


def synth(tts: KokoroTTS, text: str, voice_id: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
    style = resolve_style(tts, voice_id)
    if isinstance(style, str):
        return tts.synthesize_array(text, style, speed)
    eng = tts._engine()
    sr = 24000
    chunks = []
    for i, part in enumerate(_PAUSE.split(text)):
        if i % 2 == 1:
            chunks.append(np.zeros(int(float(part) * sr), dtype=np.float32))
            continue
        part = normalize(part, tts.lexicon)
        if part:
            samples, sr = eng.create(part, voice=style, speed=speed, lang="en-us")
            chunks.append(samples.astype(np.float32))
    return np.concatenate(chunks), sr


def _slug(voice_id: str) -> str:
    return re.sub(r"[^a-z0-9_.]+", "_", voice_id.replace("blend:", "blend_").replace("*", "x").lower())


def _encode_m4a(wav: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(out)],
                   check=True)


def _render(tts: KokoroTTS, text: str, voice_id: str, out_dir: Path, name: str, speed: float) -> tuple[Path, float, float, float]:
    import soundfile as sf

    t0 = time.time()
    audio, sr = synth(tts, text, voice_id, speed)
    dt = time.time() - t0
    audio = limit_peak(trim_silence(audio, sr))
    wav = out_dir / f"{name}.wav"
    sf.write(wav, audio, sr, subtype="PCM_16")
    m4a = out_dir / f"{name}.m4a"
    _encode_m4a(wav, m4a)
    wav.unlink()
    peak = float(20 * np.log10(max(float(np.abs(audio).max()), 1e-9)))
    return m4a, len(audio) / sr, dt, peak


def score(r: AuditionResult) -> float:
    """Puntuación 0–100 SOLO con métricas medidas: inteligibilidad (ASR) y ritmo."""
    intel = 100.0 * (1.0 - min(r.asr_wer if r.asr_wer is not None else 0.25, 0.5) * 2)
    lo, hi = WPM_RANGE
    pace = 100.0 if lo <= r.wpm <= hi else max(0.0, 100.0 - 4.0 * min(abs(r.wpm - lo), abs(r.wpm - hi)))
    return round(0.7 * intel + 0.3 * pace, 1)


def audition(tts: KokoroTTS, out_dir: Path, candidates: list[tuple[str, str]] | None = None,
             text: str | None = None, speed: float = 1.0, asr=None) -> list[AuditionResult]:  # noqa: ANN001
    """Genera una muestra por voz, mide ritmo/picos y (si hay ASR) inteligibilidad."""
    from ..quality.asr import roundtrip

    text = text or AUDITION_TEXT
    candidates = candidates or DEFAULT_CANDIDATES
    out_dir.mkdir(parents=True, exist_ok=True)
    words = len(normalize(_PAUSE.sub(" ", text)).split())
    results = []
    for voice_id, label in candidates:
        m4a, dur, dt, peak = _render(tts, text, voice_id, out_dir, _slug(voice_id), speed)
        r = AuditionResult(voice_id, label, m4a.name, round(dur, 2), round(dt, 2), round(dt / dur, 3), words,
                           round(words / dur * 60, 1), round(peak, 2))
        if asr is not None and asr.available():
            heard = asr.transcribe(m4a)
            rt = roundtrip(text, heard)
            r.asr_wer, r.asr_missing, r.asr_text = rt.wer, rt.missing, heard
        r.score = score(r)
        results.append(r)
    results.sort(key=lambda x: -(x.score or 0))
    meta = {"text": text, "speed": speed, "hardware": hardware_report(),
            "asr": "whisper-base.en (sherpa-onnx)" if asr is not None and asr.available() else None,
            "results": [asdict(r) for r in results],
            "recommendation": _recommend(results)}
    (out_dir / "auditions.json").write_text(json.dumps(meta, indent=2))
    write_gallery(out_dir, meta)
    return results


def _recommend(results: list[AuditionResult]) -> dict:
    if not results:
        return {}
    best = results[0]
    return {
        "provisional_voice": best.voice_id,
        "why": (f"mejor puntuación medida ({best.score}): WER ASR {best.asr_wer}, {best.wpm} palabras/min. "
                "Escucha las muestras antes de fijar la voz definitiva: calidez, personaje e ironía no se miden aquí."),
    }


def format_samples(tts: KokoroTTS, samples: dict[str, tuple[str, str]], out_dir: Path, asr=None) -> dict:  # noqa: ANN001
    """Compara formatos de narración (p. ej. narrador externo vs protagonista) con textos del canal.

    `samples`: {nombre: (voice_id, texto)}.
    """
    from ..quality.asr import roundtrip

    out_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, (voice_id, text) in samples.items():
        m4a, dur, _dt, _peak = _render(tts, text, voice_id, out_dir, f"format_{_slug(name)}", 1.0)
        entry = {"file": m4a.name, "voice": voice_id, "text": text, "duration_s": round(dur, 2)}
        if asr is not None and asr.available():
            entry["asr_wer"] = roundtrip(text, asr.transcribe(m4a)).wer
        out[name] = entry
    (out_dir / "format_samples.json").write_text(json.dumps(out, indent=2))
    return out


def write_gallery(out_dir: Path, meta: dict) -> Path:
    """Galería HTML local reproducible (abre los .m4a del mismo directorio)."""
    rows = []
    for r in meta["results"]:
        rows.append(
            "<tr><td><b>{v}</b><br><small>{l}</small></td><td><audio controls preload=none src='{f}'></audio></td>"
            "<td>{s}</td><td>{w}</td><td>{wpm}</td><td>{d}s</td><td>{m}</td></tr>".format(
                v=html.escape(r["voice_id"]), l=html.escape(r["label"]), f=html.escape(r["file"]),
                s=r["score"], w=r["asr_wer"], wpm=r["wpm"], d=r["duration_s"],
                m=html.escape(", ".join(r["asr_missing"] or [])),
            )
        )
    rec = meta.get("recommendation", {})
    page = f"""<!doctype html><meta charset=utf-8><title>Audiciones de voz</title>
<style>body{{font:15px system-ui;margin:24px;max-width:1100px}}td,th{{padding:6px 10px;border-bottom:1px solid #ddd;text-align:left}}</style>
<h1>Audiciones de voz</h1><p><b>Texto:</b> {html.escape(meta['text'])}</p>
<p><b>Provisional:</b> {html.escape(str(rec.get('provisional_voice')))} — {html.escape(str(rec.get('why', '')))}</p>
<table><tr><th>Voz</th><th>Muestra</th><th>Puntuación</th><th>WER ASR</th><th>Pal/min</th><th>Duración</th><th>Palabras no reconocidas</th></tr>
{''.join(rows)}</table>
<p>Fijar: <code>channel-factory voices select &lt;voice-id&gt; --channel &lt;id&gt;</code></p>"""
    path = out_dir / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def set_voice(channel_yaml: Path, voice_id: str, provisional: bool = False) -> None:
    """Fija la voz en el perfil del canal preservando comentarios del YAML."""
    text = channel_yaml.read_text(encoding="utf-8")
    key = "provisional_voice" if provisional else "selected_voice"
    new_text, n = re.subn(rf"(\n  {key}:[ \t]*)([^\s#]+)", lambda m: m.group(1) + voice_id, text, count=1)
    if n != 1:
        raise RuntimeError(f"no encontré voice.{key} en {channel_yaml}")
    if yaml.safe_load(new_text)["voice"][key] != voice_id:
        raise RuntimeError("la edición del YAML no produjo el valor esperado")
    channel_yaml.write_text(new_text, encoding="utf-8")
