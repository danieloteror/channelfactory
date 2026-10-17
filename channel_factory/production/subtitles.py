"""Subtítulos SRT/VTT desde la narración final (tiempos de escena reales).

Dentro de una escena el reparto es proporcional a caracteres (precisión de
segmento, registrada en el archivo JSON). Con alineación de palabras disponible
se pueden sustituir por tiempos exactos.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MAX_CHARS = 42  # por línea
MAX_LINES = 2


def _chunks(text: str) -> list[str]:
    text = re.sub(r"\[pause [\d.]+\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for s in sentences:
        words, cur = s.split(), ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if len(trial) <= MAX_CHARS * MAX_LINES:
                cur = trial
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return [c for c in out if c]


def _lines(chunk: str) -> str:
    if len(chunk) <= MAX_CHARS:
        return chunk
    words = chunk.split()
    best, best_diff = chunk, 10**9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if len(a) <= MAX_CHARS and len(b) <= MAX_CHARS and abs(len(a) - len(b)) < best_diff:
            best, best_diff = f"{a}\n{b}", abs(len(a) - len(b))
    return best


def cues_for_scene(text: str, start_s: float, dur_s: float) -> list[dict]:
    chunks = _chunks(text)
    total = sum(len(c) for c in chunks) or 1
    t = start_s
    cues = []
    for c in chunks:
        d = dur_s * len(c) / total
        cues.append({"start": round(t, 3), "end": round(t + d, 3), "text": _lines(c)})
        t += d
    return cues


def _ts(t: float, sep: str) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}{sep}{ms:03d}"


def write(cues: list[dict], out_base: Path) -> dict:
    srt = "\n".join(f"{i}\n{_ts(c['start'], ',')} --> {_ts(c['end'], ',')}\n{c['text']}\n" for i, c in enumerate(cues, 1))
    vtt = "WEBVTT\n\n" + "\n".join(f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')}\n{c['text']}\n" for c in cues)
    paths = {"srt": out_base.with_suffix(".srt"), "vtt": out_base.with_suffix(".vtt"), "json": out_base.with_suffix(".json")}
    paths["srt"].write_text(srt, encoding="utf-8")
    paths["vtt"].write_text(vtt, encoding="utf-8")
    paths["json"].write_text(json.dumps({"timing_precision": "segment_proportional", "cues": cues}, indent=2))
    return {k: str(v) for k, v in paths.items()}
