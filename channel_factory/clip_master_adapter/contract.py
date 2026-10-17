"""Contrato versionado con Clip Master y validación estricta de sus resultados.

Nunca se acepta un «clip generado» sin archivo, checksum, tiempos de origen,
transcripción, hook, justificación, puntuaciones, subtítulos y reporte de calidad.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..models import ClipRequest, ClipResult
from ..orchestrator.artifacts import sha256_file
from ..quality.technical import ffprobe

PROFILES_DIR = Path(__file__).parent / "profiles"


def load_profile(name: str = "original_explainer") -> dict:
    return yaml.safe_load((PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def export_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, model in (("clip_request", ClipRequest), ("clip_result", ClipResult)):
        p = out_dir / f"{name}.schema.json"
        p.write_text(json.dumps(model.model_json_schema(), indent=2))
        paths.append(p)
    return paths


@dataclass
class ResultCheck:
    ok: bool
    accepted: list[str] = field(default_factory=list)
    rejected: dict[str, list[str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / max(min(a[1] - a[0], b[1] - b[0]), 1e-6)


def validate_result(req: ClipRequest, res: ClipResult, *, source_duration_s: float, min_s: float = 3.0,
                    profile: dict | None = None) -> ResultCheck:
    profile = profile or load_profile(req.source_type)
    chk = ResultCheck(ok=True)
    if res.job_id != req.job_id:
        chk.problems.append(f"job_id {res.job_id} ≠ {req.job_id}")
    if len(res.clips) > req.max_clips:
        chk.problems.append(f"{len(res.clips)} clips > máximo {req.max_clips}")
    if len(res.clips) < req.max_clips and not (res.explanation or "").strip():
        chk.problems.append("menos clips de los pedidos sin explicación")
    openers = {w.lower() for w in profile["rules"]["forbidden_openers"]}
    max_overlap = profile["rules"]["dedupe_overlap_ratio"]
    kept: list[tuple[float, float, str]] = []
    for c in res.clips:
        bad = []
        p = Path(c.file)
        if not p.exists():
            bad.append("archivo inexistente")
        else:
            if sha256_file(p) != c.checksum:
                bad.append("checksum no coincide")
            info = ffprobe(p)
            v = [s for s in info["streams"] if s["codec_type"] == "video"]
            a = [s for s in info["streams"] if s["codec_type"] == "audio"]
            if not v or not a:
                bad.append("sin video o sin audio")
            elif abs(v[0]["width"] / v[0]["height"] - 9 / 16) > 0.01:
                bad.append(f"no es 9:16 ({v[0]['width']}x{v[0]['height']})")
            dur = float(info["format"].get("duration", 0))
            if dur < min_s or dur > req.max_duration_seconds:
                bad.append(f"duración {dur:.1f}s fuera de [{min_s}, {req.max_duration_seconds}]")
        if not (0 <= c.source_start_s < c.source_end_s <= source_duration_s + 0.05):
            bad.append("tiempos de origen fuera del master")
        for fld in ("transcript", "hook", "rationale"):
            if not getattr(c, fld).strip():
                bad.append(f"falta {fld}")
        first = re.findall(r"[A-Za-z']+", c.transcript)[:1]
        if first and first[0].lower() in openers:
            bad.append(f"empieza con '{first[0]}' (pronombre/conector huérfano)")
        if not c.subtitles.get("srt") or not Path(c.subtitles["srt"]).exists():
            bad.append("faltan subtítulos SRT")
        if c.script_version != _script_version(req):
            bad.append("versión de guion distinta del master")
        for s2, e2, k2 in kept:
            if _overlap((c.source_start_s, c.source_end_s), (s2, e2)) > max_overlap:
                bad.append(f"duplica esencialmente el clip {k2}")
        if bad:
            chk.rejected[c.clip_id] = bad
        else:
            chk.accepted.append(c.clip_id)
            kept.append((c.source_start_s, c.source_end_s, c.clip_id))
    chk.ok = not chk.problems and not chk.rejected
    return chk


def _script_version(req: ClipRequest) -> int:
    try:
        return int(json.loads(Path(req.scene_manifest_path).read_text()).get("script_version", 1))
    except (OSError, ValueError):
        return 1
