"""Pistas de segmentos para Clip Master desde la estructura del guion.

Channel Factory conoce los beats (pregunta, ejemplo, giro, resolución) y los
límites reales de escena; se los pasa a Clip Master para que corte en unidades
narrativas, sin reimplementar su selección.
"""

from __future__ import annotations

import re

from .contract import load_profile

UNIT_START = {"hook", "question", "setup"}
UNIT_END = {"resolution", "twist", "nuance", "example"}


def candidate_hints(scenes_timeline: list[dict], preferred: tuple[int, int], max_s: int) -> list[dict]:
    """Agrupa escenas consecutivas en unidades que empiezan en pregunta/gancho y acaban en resolución."""
    hints = []
    n = len(scenes_timeline)
    for i, s in enumerate(scenes_timeline):
        if s["beat"] not in UNIT_START and s.get("clip_hint") not in ("question", "surprise"):
            continue
        for j in range(i, n):
            e = scenes_timeline[j]
            dur = e["end"] - s["narration_start"]
            if dur > max_s:
                break
            closes = e["beat"] in UNIT_END or e.get("clip_hint") in ("resolution", "example")
            if closes and dur >= min(preferred[0], 20):
                hints.append({
                    "start_s": s["narration_start"], "end_s": round(e["end"] - 0.05, 3),
                    "scenes": [x["scene_id"] for x in scenes_timeline[i:j + 1]],
                    "moment_type": s.get("clip_hint") or ("question" if s["beat"] == "question" else "example"),
                    "within_preferred": preferred[0] <= dur <= preferred[1], "duration_s": round(dur, 2),
                })
                break
    return hints


def boundary_check(segments: list[dict], start: float, end: float, *, tol: float = 0.08,
                   profile: dict | None = None) -> list[str]:
    """Comprueba que [start, end] empieza y termina en límites de frase de la transcripción.

    `segments`: [{start, end, text}] con frases o escenas. Devuelve problemas (vacío = ok).
    """
    profile = profile or load_profile()
    problems = []
    starts = [s["start"] for s in segments]
    ends = [s["end"] for s in segments]
    if not any(abs(start - x) <= tol for x in starts):
        problems.append(f"inicio {start:.2f}s no coincide con el comienzo de una frase")
    if not any(abs(end - x) <= tol + profile["rules"]["max_trailing_silence_s"] for x in ends):
        problems.append(f"fin {end:.2f}s no coincide con el final de una frase")
    first = next((s for s in segments if abs(s["start"] - start) <= tol), None)
    if first:
        w = re.findall(r"[A-Za-z']+", first["text"])[:1]
        if w and w[0].lower() in {x.lower() for x in profile["rules"]["forbidden_openers"]}:
            problems.append(f"abre con '{w[0]}': pronombre/conector huérfano")
    return problems


def sentence_segments(transcript_segments: list[dict]) -> list[dict]:
    """Divide segmentos de escena en frases con tiempos proporcionales (precisión de segmento)."""
    out = []
    for seg in transcript_segments:
        text = re.sub(r"\[pause [\d.]+\]", " ", seg["text"]).strip()
        parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p]
        total = sum(len(p) for p in parts) or 1
        t = seg["start"]
        for p in parts:
            d = (seg["end"] - seg["start"]) * len(p) / total
            out.append({"start": round(t, 3), "end": round(t + d, 3), "text": p})
            t += d
    return out
