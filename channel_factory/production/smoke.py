"""Prueba de humo del pipeline con un episodio sintético (formas generadas, sin contenido de canal).

Uso: `channel-factory doctor --smoke` o `python -m channel_factory.production.smoke <dir>`.
Si Kokoro no está disponible usa un TTS de tonos (marcado como tal) para probar el resto.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml
from PIL import Image, ImageDraw

from ..providers.base import SynthResult


class ToneTTS:
    """TTS de prueba: tonos con la duración aproximada del texto. NO es una voz."""

    provider_id = "tone_test_double"

    def synthesize(self, text: str, voice_id: str, out_path: Path, speed: float = 1.0) -> SynthResult:
        sr = 24000
        dur = max(0.8, len(text.split()) / 2.6 / speed)
        t = np.arange(int(dur * sr)) / sr
        a = 0.2 * np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, a.astype(np.float32), sr)
        return SynthResult(out_path, dur, sr, voice_id)


def _assets(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    bg = Image.new("RGB", (3840, 2160), (28, 38, 64))
    dr = ImageDraw.Draw(bg)
    for i in range(0, 3840, 240):
        dr.rectangle([i, 1500 - (i * 7) % 600, i + 200, 2160], fill=(40 + i % 50, 60, 96))
    dr.ellipse([3000, 200, 3400, 600], fill=(250, 210, 120))
    bg.save(d / "bg.png")
    for name, color, mouth in (("hero_neutral", (230, 150, 70), 0), ("hero_surprised", (230, 150, 70), 1)):
        im = Image.new("RGBA", (800, 1400), (0, 0, 0, 0))
        g = ImageDraw.Draw(im)
        g.rounded_rectangle([200, 600, 600, 1350], radius=120, fill=color)
        g.ellipse([150, 100, 650, 600], fill=color)
        g.ellipse([290, 280, 350, 340], fill=(20, 20, 20))
        g.ellipse([450, 280, 510, 340], fill=(20, 20, 20))
        if mouth:
            g.ellipse([360, 420, 440, 520], fill=(60, 20, 20))
        else:
            g.arc([340, 400, 460, 480], 20, 160, fill=(60, 20, 20), width=12)
        im.save(d / f"{name}.png")


def _music(path: Path, seconds: float = 40.0) -> None:
    sr = 48000
    t = np.arange(int(seconds * sr)) / sr
    chord = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 277.18, 329.63)) / 3
    env = 0.6 + 0.4 * np.sin(2 * np.pi * t / 4)
    sf.write(path, (0.25 * chord * env).astype(np.float32), sr)


def build_episode(root: Path) -> Path:
    ep = root / "episodes" / "smoke-001"
    _assets(ep / "assets")
    _music(ep / "assets" / "music.wav")
    manifest = {
        "channel_id": "smoke", "episode_id": "smoke-001", "title": "Smoke test", "language": "en-US",
        "music": {"path": "assets/music.wav", "license": "generated procedurally (no third-party rights)",
                  "source": "channel_factory.production.smoke", "gain_db": -24},
        "thumbnail": {"scene_id": "s02", "title": "WHY PRICES RISE"},
        "scenes": [
            {"scene_id": "s01", "beat": "hook", "transition_in": "fade_black", "clip_hint": "question",
             "text": "Why does the same coffee cost more every year? [pause 0.3] Let's find out.",
             "visual": {"background": "assets/bg.png", "camera": {"from": [0.5, 0.5, 1.0], "to": [0.45, 0.55, 1.2]},
                        "characters": [{"id": "hero", "image": "assets/hero_neutral.png", "x": 0.35, "y": 0.62,
                                        "height": 0.62, "idle": "bob", "enter": {"at": 0.1, "anim": "pop"},
                                        "swaps": [{"at_text": "Let's", "image": "assets/hero_surprised.png"}]}],
                        "overlays": [{"type": "text", "text": "Same coffee. Higher price.", "at_text": "cost",
                                      "x": 0.68, "y": 0.3, "size": 70, "box": "#111827", "anim": "pop"}],
                        "vertical_roi": [0.15, 0.0, 0.4, 1.0]}},
            {"scene_id": "s02", "beat": "mechanism", "transition_in": "crossfade", "clip_hint": "example",
             "text": "Five years ago it cost 2.50. Today it costs 3.10. That's a 24% jump.",
             "visual": {"background": "assets/bg.png", "camera": {"from": [0.55, 0.5, 1.1], "to": [0.5, 0.5, 1.0]},
                        "characters": [{"id": "hero", "image": "assets/hero_neutral.png", "x": 0.2, "y": 0.62,
                                        "height": 0.6}],
                        "overlays": [{"type": "bar_chart", "at_text": "Five", "x": 0.42, "y": 0.18, "w": 0.48,
                                      "h": 0.6, "data": [["Then", 2.5], ["Now", 3.1]], "format": ".2f",
                                      "colors": ["#60A5FA", "#F87171"], "duration": 1.2},
                                     {"type": "counter", "at_text": "24%", "from": 0, "to": 24, "suffix": "%",
                                      "x": 0.66, "y": 0.88, "size": 90, "duration": 0.8}],
                        "vertical_roi": [0.05, 0.0, 0.32, 1.0],
                        "vertical_layout": {"layers": {"hero": {"x": 0.5, "y": 0.72, "height": 0.45}},
                                            "roi": [0.3, 0.0, 0.4, 1.0]}}},
        ],
    }
    (ep / "episode.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return ep


def run(root: Path, tts=None, voice: str = "af_heart") -> dict:  # noqa: ANN001
    from .episode import EpisodeWorkspace, assemble, mix_audio, narrate, render, thumbnail
    from ..quality.technical import check_video

    t0 = time.time()
    ep = build_episode(root)
    ws = EpisodeWorkspace(ep)
    tts = tts or ToneTTS()
    rep = {"tts": getattr(tts, "provider_id", "?"), "narration": narrate(ws, tts, voice)}
    scenes = render(ws, width=1280, height=720, fps=30)
    rep["mix"] = mix_audio(ws, lufs=-14.0, true_peak=-1.5, music_db=-24, ducking=True)
    rep["assemble"] = assemble(ws, scenes)
    rep["thumbnail"] = thumbnail(ws, scenes)
    rep["qa"] = check_video(Path(rep["assemble"]["master"]), width=1280, height=720, fps=30, lufs=-14.0,
                            true_peak_max=-1.5).to_dict()
    vert = render(ws, width=540, height=960, fps=30, vertical=True)
    rep["vertical"] = vert
    rep["seconds"] = round(time.time() - t0, 1)
    return rep


if __name__ == "__main__":
    import json

    out = run(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("var/smoke"))
    print(json.dumps(out, indent=2, default=str))
