"""Mezcla: narración al frente, música baja con ducking, SFX discretos y loudness objetivo.

La pista de narración se arma con los WAV por escena en sus tiempos reales.
La normalización es de dos pasadas (`loudnorm` medido + lineal) con true peak limitado.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 48000


def _read_mono(path: Path, sr: int = SR) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def build_narration(segments: list[tuple[float, Path]], total_s: float, out: Path) -> Path:
    """Coloca cada WAV en su instante (s) sobre una pista de `total_s` segundos."""
    track = np.zeros(int(total_s * SR) + SR, dtype=np.float32)
    for start, wav in segments:
        a = _read_mono(wav)
        i = int(round(start * SR))
        j = min(i + len(a), len(track))
        track[i:j] += a[: j - i]
    track = track[: int(total_s * SR)]
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, track, SR, subtype="PCM_24")
    return out


def build_sfx(events: list[tuple[float, Path, float]], total_s: float, out: Path) -> Path | None:
    if not events:
        return None
    track = np.zeros(int(total_s * SR) + SR, dtype=np.float32)
    for start, wav, gain_db in events:
        a = _read_mono(wav) * (10 ** (gain_db / 20))
        i = int(round(start * SR))
        j = min(i + len(a), len(track))
        track[i:j] += a[: j - i]
    sf.write(out, track[: int(total_s * SR)], SR, subtype="PCM_24")
    return out


def _loudnorm_measure(path: Path, lufs: float, tp: float) -> dict:
    log = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
         f"loudnorm=I={lufs}:TP={tp}:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", log, re.S)
    if not m:
        raise RuntimeError("loudnorm no devolvió medidas")
    return json.loads(m.group(0))


def mix(narration: Path, out: Path, *, total_s: float, music: Path | None = None, music_db: float = -22.0,
        ducking: bool = True, sfx: Path | None = None, lufs: float = -14.0, true_peak: float = -1.5) -> dict:
    """Mezcla final a WAV 48 kHz estéreo normalizado. Devuelve medidas de loudness."""
    inputs = ["-i", str(narration)]
    filt = ["[0:a]aformat=channel_layouts=mono,asplit=2[narr][key]"]
    mix_in = ["[narr]"]
    idx = 1
    if music is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        fade_out = max(total_s - 2.5, 0)
        filt.append(
            f"[{idx}:a]aformat=channel_layouts=stereo,atrim=0:{total_s:.3f},volume={music_db}dB,"
            f"afade=t=in:d=1.5,afade=t=out:st={fade_out:.3f}:d=2.5[mus]"
        )
        if ducking:
            filt.append("[mus][key]sidechaincompress=threshold=0.02:ratio=6:attack=25:release=450:makeup=1[mduck]")
            mix_in.append("[mduck]")
        else:
            filt.append("[key]anullsink")
            mix_in.append("[mus]")
        idx += 1
    else:
        filt.append("[key]anullsink")
    if sfx is not None:
        inputs += ["-i", str(sfx)]
        mix_in.append(f"[{idx}:a]")
        idx += 1
    filt.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0:duration=first,"
                "aformat=sample_rates=48000:channel_layouts=stereo[pre]")
    pre = out.with_suffix(".pre.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filt),
                    "-map", "[pre]", "-t", f"{total_s:.3f}", "-c:a", "pcm_s24le", str(pre)], check=True)
    m = _loudnorm_measure(pre, lufs, true_peak)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(pre), "-af",
         f"loudnorm=I={lufs}:TP={true_peak}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
         f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
         "linear=true:print_format=summary,aresample=48000",
         "-c:a", "pcm_s24le", str(out)],
        check=True,
    )
    pre.unlink(missing_ok=True)
    return {"measured_input": m, "target_lufs": lufs, "target_tp": true_peak}
