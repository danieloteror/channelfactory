"""DOBLE DE PRUEBA de Clip Master (no es Clip Master).

Lee una ClipRequest, corta en vertical los segmentos sugeridos por `candidate_hints`
con FFmpeg y escribe un ClipResult válido. Solo lo usan los tests del adaptador.
Uso: python fake_clipmaster.py <request.json> <out_dir>
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main(req_path: str, out_dir: str) -> None:
    req = json.loads(Path(req_path).read_text())
    out = Path(out_dir)
    transcript = json.loads(Path(req["transcript_path"]).read_text())["segments"]
    version = json.loads(Path(req["scene_manifest_path"]).read_text()).get("script_version", 1)
    clips = []
    for i, h in enumerate(req["candidate_hints"][: req["max_clips"]], 1):
        f = out / f"clip_{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(h["start_s"]), "-to", str(h["end_s"]),
                        "-i", req["source_video"], "-vf", "crop=ih*9/16:ih,scale=540:960", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "aac", str(f)], check=True)
        text = " ".join(s["text"] for s in transcript if s["start"] >= h["start_s"] - 0.05 and s["end"] <= h["end_s"] + 0.1)
        srt = out / f"clip_{i}.srt"
        srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{text[:40]}\n")
        clips.append({
            "clip_id": f"c{i}", "file": str(f), "checksum": hashlib.sha256(f.read_bytes()).hexdigest(),
            "source_start_s": h["start_s"], "source_end_s": h["end_s"], "script_version": version,
            "transcript": text, "hook": text.split(".")[0][:80], "rationale": f"unidad {h['moment_type']}",
            "scores": {"hook": 0.7, "standalone": 0.8, "payoff": 0.7, "visual_legibility": 0.8, "overall": 0.75},
            "subtitles": {"srt": str(srt)}, "thumbnail": None, "quality_report": {"ok": True},
        })
    res = {"schema_version": "1.0", "job_id": req["job_id"], "status": "ok" if clips else "none", "clips": clips,
           "explanation": None if len(clips) >= req["max_clips"] else "menos unidades narrativas completas que el máximo"}
    (out / "clip_result.json").write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
