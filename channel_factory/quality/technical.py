"""QA técnica de medios con ffprobe/ffmpeg: duración, resolución, codecs, audio,
decodificación, frames negros, silencios, loudness y picos."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def _ffmpeg_filter_log(path: Path, af: str | None = None, vf: str | None = None) -> str:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path)]
    if vf:
        cmd += ["-vf", vf]
    else:
        cmd += ["-vn"]
    if af:
        cmd += ["-af", af]
    else:
        cmd += ["-an"]
    cmd += ["-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True, text=True).stderr


def loudness(path: Path) -> dict:
    """Loudness integrada (LUFS), rango y true peak (dBTP) con ebur128."""
    log = _ffmpeg_filter_log(path, af="ebur128=peak=true")
    summary = log[log.rfind("Summary:"):] if "Summary:" in log else log

    def grab(label: str) -> float | None:
        m = re.search(rf"{label}:\s+(-?[\d.]+|-inf)", summary)
        return None if not m or m.group(1) == "-inf" else float(m.group(1))

    return {"integrated_lufs": grab("I"), "lra_lu": grab("LRA"), "true_peak_dbtp": grab("Peak")}


def silences(path: Path, noise_db: float = -45.0, min_s: float = 1.2) -> list[tuple[float, float]]:
    log = _ffmpeg_filter_log(path, af=f"silencedetect=n={noise_db}dB:d={min_s}")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", log)]
    return list(zip(starts, ends + [float("nan")] * (len(starts) - len(ends))))


def black_frames(path: Path, min_s: float = 0.5) -> list[tuple[float, float]]:
    log = _ffmpeg_filter_log(path, vf=f"blackdetect=d={min_s}:pix_th=0.10")
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)]


def decodes_cleanly(path: Path) -> tuple[bool, str]:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return (r.returncode == 0 and not r.stderr.strip(), r.stderr.strip()[:1000])


@dataclass
class QAReport:
    path: str
    ok: bool
    checks: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def check_video(
    path: Path,
    *,
    width: int,
    height: int,
    fps: int | None = None,
    min_s: float | None = None,
    max_s: float | None = None,
    lufs: float | None = None,
    lufs_tol: float = 1.5,
    true_peak_max: float | None = None,
    allow_silence_s: float = 2.5,
    require_audio: bool = True,
    deep: bool = True,
) -> QAReport:
    rep = QAReport(str(path), ok=True)
    if not Path(path).exists():
        rep.ok = False
        rep.problems.append("archivo inexistente")
        return rep
    info = ffprobe(path)
    v = [s for s in info["streams"] if s["codec_type"] == "video"]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    dur = float(info["format"].get("duration", 0))
    rep.checks.update(duration_s=round(dur, 3), video_streams=len(v), audio_streams=len(a),
                      size_bytes=int(info["format"].get("size", 0)))
    if not v:
        rep.problems.append("sin stream de video")
    else:
        vs = v[0]
        rep.checks.update(width=vs["width"], height=vs["height"], vcodec=vs["codec_name"], pix_fmt=vs.get("pix_fmt"))
        if (vs["width"], vs["height"]) != (width, height):
            rep.problems.append(f"resolución {vs['width']}x{vs['height']} ≠ {width}x{height}")
        if fps:
            num, den = (int(x) for x in vs.get("avg_frame_rate", "0/1").split("/"))
            real = num / den if den else 0
            rep.checks["fps"] = round(real, 3)
            if abs(real - fps) > 0.05:
                rep.problems.append(f"fps {real:.2f} ≠ {fps}")
        if vs.get("pix_fmt") not in (None, "yuv420p"):
            rep.warnings.append(f"pix_fmt {vs.get('pix_fmt')} (yuv420p es lo más compatible)")
    if require_audio and not a:
        rep.problems.append("sin stream de audio")
    elif a:
        rep.checks.update(acodec=a[0]["codec_name"], sample_rate=int(a[0].get("sample_rate", 0)))
    if min_s is not None and dur < min_s:
        rep.problems.append(f"duración {dur:.1f}s < mínimo {min_s}s")
    if max_s is not None and dur > max_s:
        rep.problems.append(f"duración {dur:.1f}s > máximo {max_s}s")
    if deep:
        ok, err = decodes_cleanly(path)
        rep.checks["decodes_cleanly"] = ok
        if not ok:
            rep.problems.append(f"errores de decodificación: {err[:200]}")
        if v:
            blacks = black_frames(path)
            rep.checks["black_segments"] = blacks
            # Un fundido final corto es aceptable; negros largos en medio no.
            for s, e in blacks:
                if e - s > 1.0 and e < dur - 0.2:
                    rep.problems.append(f"negro anómalo {s:.1f}–{e:.1f}s")
        if a:
            sil = silences(path, min_s=allow_silence_s)
            rep.checks["silences"] = sil
            for s, e in sil:
                rep.problems.append(f"silencio injustificado {s:.1f}–{e if e == e else dur:.1f}s")
            loud = loudness(path)
            rep.checks["loudness"] = loud
            if lufs is not None and loud["integrated_lufs"] is not None and abs(loud["integrated_lufs"] - lufs) > lufs_tol:
                rep.problems.append(f"loudness {loud['integrated_lufs']} LUFS fuera de {lufs}±{lufs_tol}")
            if true_peak_max is not None and loud["true_peak_dbtp"] is not None and loud["true_peak_dbtp"] > true_peak_max + 0.3:
                rep.problems.append(f"true peak {loud['true_peak_dbtp']} dBTP > {true_peak_max}")
    rep.ok = not rep.problems
    return rep
