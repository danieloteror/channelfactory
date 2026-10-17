"""Riesgos 7, 8, 9 y 10: invalidación selectiva, límites de clips, reencuadre vertical y Clip Master."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from channel_factory.clip_master_adapter.adapter import build_request, run_and_validate
from channel_factory.clip_master_adapter.contract import validate_result
from channel_factory.clip_master_adapter.hints import boundary_check, sentence_segments
from channel_factory.clip_master_adapter.transport import CliTransport, get_transport
from channel_factory.config import ClipMasterCfg, ClipsCfg
from channel_factory.db import DB
from channel_factory.models import ClipRequest, ClipResult, Scene
from channel_factory.orchestrator.artifacts import Artifacts
from channel_factory.orchestrator.worker import Blocked
from channel_factory.production.compositor import (_build_layers, camera_rect, clipped_essentials, render_scene)
from channel_factory.production.episode import EpisodeWorkspace, assemble, mix_audio, narrate, render
from channel_factory.production.smoke import ToneTTS, build_episode
from channel_factory.quality.technical import check_video, ffprobe

FAKE = Path(__file__).parent / "fake_clipmaster.py"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):  # noqa: ANN001, ANN201
    root = tmp_path_factory.mktemp("ep")
    ws = EpisodeWorkspace(build_episode(root))
    db = DB(root / "a.sqlite")
    art = Artifacts(db)
    narrate(ws, ToneTTS(), "test", artifacts=art)
    scenes = render(ws, width=320, height=180, fps=15, artifacts=art)
    mix_audio(ws, lufs=-14, true_peak=-1.5, music_db=-24, ducking=True)
    out = assemble(ws, scenes)
    return {"ws": ws, "db": db, "art": art, "out": out}


def test_master_passes_technical_qa(rendered) -> None:  # noqa: ANN001
    rep = check_video(Path(rendered["out"]["master"]), width=320, height=180, fps=15, lufs=-14, true_peak_max=-1.5)
    assert rep.ok, rep.problems


def test_script_change_invalidates_only_that_scene(rendered) -> None:  # noqa: ANN001
    ws, art = rendered["ws"], rendered["art"]
    data = yaml.safe_load(ws.manifest_path.read_text())
    data["scenes"][1]["text"] = "Five years ago it cost 2.50. Today it costs 3.10. That is a 24% jump, give or take."
    data["script_version"] = 2
    ws.manifest_path.write_text(yaml.safe_dump(data, sort_keys=False))
    rep = narrate(ws, ToneTTS(), "test", artifacts=art)
    cached = {s["scene_id"]: s["cached"] for s in rep["scenes"]}
    assert cached == {"s01": True, "s02": False}
    vids = render(ws, width=320, height=180, fps=15, artifacts=art)
    assert {v["scene_id"]: v["cached"] for v in vids} == {"s01": True, "s02": False}
    snap = yaml.safe_load((ws.render_dir / "episode.snapshot.yaml").read_text())
    assert snap["script_version"] == 1  # el master anterior conserva su versión trazable


def test_clip_master_end_to_end_with_test_double(rendered, tmp_path) -> None:  # noqa: ANN001
    ws = rendered["ws"]
    cfg = ClipsCfg(preferred_seconds=(5, 30), max_per_episode=3)
    req = build_request(ws.render_dir, channel_id="smoke", episode_id="smoke-001", job_id="j1", clips_cfg=cfg)
    assert req.allow_publish is False and req.candidate_hints
    tr = CliTransport([sys.executable, str(FAKE), "{request}", "{out_dir}"], None, 120)
    rep = run_and_validate(tr, req, tmp_path / "clips")
    assert rep["ok"], rep
    assert rep["result"]["explanation"]  # pidió 3, hay 1 unidad completa → explica por qué


def _result_with(req: ClipRequest, clip: dict, n: int = 1) -> ClipResult:
    return ClipResult.model_validate({"job_id": req.job_id, "status": "ok", "explanation": "x",
                                      "clips": [{**clip, "clip_id": f"c{i}"} for i in range(n)]})


def test_duplicates_and_orphan_openers_rejected(rendered, tmp_path) -> None:  # noqa: ANN001
    ws = rendered["ws"]
    cfg = ClipsCfg(preferred_seconds=(5, 30), max_per_episode=3)
    req = build_request(ws.render_dir, channel_id="smoke", episode_id="smoke-001", job_id="j2", clips_cfg=cfg)
    tr = CliTransport([sys.executable, str(FAKE), "{request}", "{out_dir}"], None, 120)
    good = run_and_validate(tr, req, tmp_path / "c")["result"]["clips"][0]
    dur = float(ffprobe(Path(req.source_video))["format"]["duration"])
    dup = validate_result(req, _result_with(req, good, 2), source_duration_s=dur)
    assert "c1" in dup.rejected and any("duplica" in p for p in dup.rejected["c1"])
    orphan = validate_result(req, _result_with(req, {**good, "transcript": "This is why prices rise."}),
                             source_duration_s=dur)
    assert any("huérfano" in p for p in orphan.rejected["c0"])


def test_boundaries_are_sentence_aligned(rendered) -> None:  # noqa: ANN001
    tr = json.loads((rendered["ws"].render_dir / "transcript.json").read_text())
    sents = sentence_segments(tr["segments"])
    assert boundary_check(sents, sents[0]["start"], sents[1]["end"]) == []
    mid = (sents[0]["start"] + sents[0]["end"]) / 2
    assert any("inicio" in p for p in boundary_check(sents, mid, sents[1]["end"]))


def test_clip_master_never_publishes_and_needs_config() -> None:
    with pytest.raises(ValidationError):
        ClipMasterCfg(allow_publish=True)
    with pytest.raises(ValidationError):
        ClipRequest(job_id="j", channel_id="c", episode_id="e", source_video="v", source_checksum="x",
                    transcript_path="t", scene_manifest_path="s", allow_publish=True)
    with pytest.raises(Blocked):
        get_transport(ClipMasterCfg())
    with pytest.raises(Blocked):
        CliTransport(["/no/such/clipmaster"], "/no/such/dir").check()


def test_vertical_recomposes_when_roi_cuts_character(tmp_path) -> None:  # noqa: ANN001
    ep = build_episode(tmp_path)
    data = yaml.safe_load((ep / "episode.yaml").read_text())
    sc = data["scenes"][1]
    sc["duration_s"] = 1.0
    sc["visual"]["vertical_roi"] = [0.62, 0.0, 0.32, 1.0]  # el personaje (x=0.2) queda fuera
    scene = Scene.model_validate(sc)
    rep = render_scene(scene, ep, tmp_path / "v.mp4", width=270, height=480, fps=10, vertical=True)
    assert rep.recomposed and rep.clipped_layers == []
    # Sin recomposición disponible, el corte se detecta y se informa (no se oculta).
    sc["visual"]["vertical_layout"] = None
    scene2 = Scene.model_validate(sc)
    layers = _build_layers(scene2, ep, 1.0, None)
    rect = camera_rect(scene2.visual.camera, 0, 1.0, (3840, 2160), 270 / 480, tuple(sc["visual"]["vertical_roi"]))
    assert clipped_essentials(layers, (3840, 2160), rect) == ["hero"]


def test_scene_with_style_video_background(tmp_path) -> None:  # noqa: ANN001
    """Un MP4 de una skill de estilo sirve de fondo; cámara y capas siguen funcionando encima."""
    from .conftest import make_video

    ep = build_episode(tmp_path)
    make_video(ep / "assets" / "style_clip.mp4", 640, 360, 1.0, audio=False)
    sc = yaml.safe_load((ep / "episode.yaml").read_text())["scenes"][0]
    sc.update(duration_s=1.5, transition_in="cut")
    sc["visual"]["video"] = {"path": "assets/style_clip.mp4", "loop": False, "style": "hyperframes"}
    sc["visual"]["overlays"] = []
    rep = render_scene(Scene.model_validate(sc), ep, tmp_path / "v.mp4", width=320, height=180, fps=10)
    assert rep.frames == 20  # 0,15 entrada + 1,5 narración + 0,35 salida
    info = ffprobe(tmp_path / "v.mp4")
    assert float(info["format"]["duration"]) >= 1.9
