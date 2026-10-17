from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from channel_factory.config import FactoryConfig, settings_from
from channel_factory.db import DB

BRAND = "1000001"  # marca ficticia del canal de prueba
OTHER_BRAND = "1000002"  # marca protegida de otro flujo


def make_video(path: Path, w: int = 1280, h: int = 720, dur: float = 4.0, audio: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30:duration={dur}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def channel_profile(tmp: Path, **pub) -> dict:
    prof = {
        "id": "testchan", "name": "Test Channel", "enabled": True, "niche": "test",
        "voice": {"provider": "kokoro", "provisional_voice": "af_heart"},
        "publishing": {"mode": "draft", "timezone": "America/New_York", "metricool_brand_id": BRAND,
                       "youtube_destination_id": "UC_TEST", "tiktok_destination_id": "tt_test", **pub},
    }
    d = tmp / "channels" / "testchan"
    d.mkdir(parents=True, exist_ok=True)
    (d / "channel.yaml").write_text(yaml.safe_dump(prof))
    return prof


@pytest.fixture
def settings(tmp_path: Path):
    channel_profile(tmp_path)
    cfg = FactoryConfig.model_validate({
        "project": {"state_dir": str(tmp_path / "var")},
        "publishing": {"enabled": True, "mode": "autopilot"},
        "protected_brand_ids": [OTHER_BRAND],
        "channels": {"testchan": {"enabled": True, "profile": "channels/testchan/channel.yaml"}},
    })
    return settings_from(tmp_path, cfg)


@pytest.fixture
def db(tmp_path: Path) -> DB:
    return DB(tmp_path / "var" / "test.sqlite")


@pytest.fixture
def brands() -> list[dict]:
    return [
        {"id": int(BRAND), "label": "testchan", "networksData": {"youtubeData": "UC_TEST", "tiktokData": "tt_test"}},
        {"id": int(OTHER_BRAND), "label": "otherflow", "networksData": {"youtubeData": "UC_OTHER"}},
        {"id": 1000003, "label": "empty", "networksData": {}},
    ]
