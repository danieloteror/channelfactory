"""Construye la solicitud de recortes desde un episodio renderizado y valida la respuesta."""

from __future__ import annotations

import json
from pathlib import Path

from ..models import ClipRequest
from ..orchestrator.artifacts import sha256_file
from ..quality.technical import ffprobe
from .contract import validate_result
from .hints import candidate_hints


def build_request(render_dir: Path, *, channel_id: str, episode_id: str, job_id: str, clips_cfg,  # noqa: ANN001
                  language: str = "en-US") -> ClipRequest:
    master = render_dir / "master_clean.mp4"
    scenes = json.loads((render_dir / "scenes.json").read_text())
    pref = tuple(clips_cfg.preferred_seconds)
    return ClipRequest(
        job_id=job_id, channel_id=channel_id, episode_id=episode_id, source_video=str(master.resolve()),
        source_checksum=sha256_file(master), language=language,
        transcript_path=str((render_dir / "transcript.json").resolve()),
        scene_manifest_path=str((render_dir / "scenes.json").resolve()),
        preferred_duration_seconds=pref, max_duration_seconds=clips_cfg.youtube_max_seconds,
        max_clips=clips_cfg.max_per_episode,
        candidate_hints=candidate_hints(scenes["scenes"], pref, clips_cfg.youtube_max_seconds),
    )


def run_and_validate(transport, req: ClipRequest, work_dir: Path) -> dict:  # noqa: ANN001
    res = transport.run(req, work_dir)
    dur = float(ffprobe(Path(req.source_video))["format"]["duration"])
    chk = validate_result(req, res, source_duration_s=dur)
    report = {"result": res.model_dump(mode="json"), "accepted": chk.accepted, "rejected": chk.rejected,
              "problems": chk.problems, "ok": chk.ok}
    (work_dir / "clip_validation.json").write_text(json.dumps(report, indent=2))
    return report
