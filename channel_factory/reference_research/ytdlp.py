"""Envoltorio de yt-dlp: metadatos, subtítulos y (solo si se autoriza) medios para análisis.

El texto de páginas, descripciones y subtítulos es dato NO confiable: nunca se
interpreta como instrucción ni se ejecuta.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class YtDlpBlocked(RuntimeError):
    pass


def _bin() -> list[str]:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401

        return ["python3", "-m", "yt_dlp"]
    except ImportError as e:
        raise YtDlpBlocked("yt-dlp no instalado (pip install '.[analysis]')") from e


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    r = subprocess.run([*_bin(), "--no-warnings", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        msg = r.stderr.strip()[-600:]
        if "Unable to download" in msg or "HTTP Error 403" in msg or "Connection" in msg:
            raise YtDlpBlocked(f"sin acceso a YouTube desde este entorno: {msg}")
        raise RuntimeError(msg)
    return r


def metadata(url: str) -> dict:
    return json.loads(_run(["-J", "--skip-download", url]).stdout)


def playlist_json(url: str, limit: int = 500) -> dict:
    """JSON plano de una pestaña/lista (título, duración y visitas por entrada; sin fecha)."""
    r = _run(["--flat-playlist", "-J", "--playlist-end", str(limit), url])
    return json.loads(r.stdout)


def flat_playlist(url: str, limit: int = 500) -> list[dict]:
    return playlist_json(url, limit).get("entries", [])


def subtitles(url: str, out_dir: Path, langs: str = "es.*,en.*") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", langs, "--sub-format", "vtt",
          "-o", str(out_dir / "%(id)s.%(ext)s"), url])
    return sorted(out_dir.glob("*.vtt"))


def download_for_analysis(url: str, out_dir: Path, max_height: int = 720) -> Path:
    """Descarga un medio SOLO para análisis local cuando el uso está permitido; no se redistribuye."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(["-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]", "--merge-output-format", "mp4",
          "-o", str(out_dir / "%(id)s.%(ext)s"), url], timeout=3600)
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise RuntimeError("yt-dlp no produjo un mp4")
    return files[-1]


def vtt_to_text(path: Path) -> str:
    """Texto plano de un VTT (quita tiempos, etiquetas y repeticiones de subtítulos automáticos)."""
    import re

    lines, last = [], None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "-->" in line or not line.strip() or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean and clean != last:
            lines.append(clean)
            last = clean
    return " ".join(lines)
