"""Qué le funcionó más a un canal: comparación determinista de rasgos entre los videos
que rinden por encima de su cohorte y el resto.

Produce OBSERVACIONES con números (medianas, proporciones, tamaño de muestra). El «por qué»
es una hipótesis que escribe el analista tras revisar miniaturas, keyframes y transcripciones;
aquí solo se marca la fuerza de la evidencia. Correlación ≠ causalidad.
"""

from __future__ import annotations

import re
from datetime import datetime
from statistics import median

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

_FEATURES = {
    "titulo_pregunta": lambda t: "?" in t or t.strip().lower().startswith(("por qué", "porqué", "why", "qué ", "what ", "cómo", "how")),
    "titulo_con_numero": lambda t: bool(re.search(r"\d", t)),
    "titulo_con_porque": lambda t: bool(re.search(r"\b(por qu[eé]|porqu[eé]|why)\b", t, re.I)),
    "titulo_con_coma_o_dos_puntos": lambda t: bool(re.search(r"[,:|—-]", t)),
    "titulo_mayusculas_enfaticas": lambda t: bool(re.search(r"\b[A-ZÁÉÍÓÚÑ]{3,}\b", t)),
    "titulo_con_emoji": lambda t: bool(re.search(r"[\U0001F300-\U0001FAFF☀-➿]", t)),
}


def _title_words(t: str) -> int:
    return len(re.findall(r"\w+", t))


def _bucket_duration(s: float | None) -> str | None:
    if s is None:
        return None
    for lim, lbl in ((60, "<1 min"), (180, "1-3 min"), (480, "3-8 min"), (900, "8-15 min"), (1800, "15-30 min")):
        if s < lim:
            return lbl
    return "30+ min"


def _strength(n_top: int, n_rest: int, diff: float) -> str:
    n = min(n_top, n_rest)
    if n < 5:
        return "débil (muestra pequeña)"
    if abs(diff) >= 0.25 and n >= 10:
        return "moderada"
    return "débil"


def _recurring_phrases(titles: list[str], min_count: int = 3) -> list[tuple[str, int]]:
    """N-gramas (2–4 palabras) repetidos en títulos: fórmulas de título del canal."""
    counts: dict[str, int] = {}
    for t in titles:
        words = re.findall(r"\w+", t.lower())
        seen = set()
        for n in (2, 3, 4):
            for i in range(len(words) - n + 1):
                g = " ".join(words[i:i + n])
                if g not in seen:
                    seen.add(g)
                    counts[g] = counts.get(g, 0) + 1
    out = sorted(((g, c) for g, c in counts.items() if c >= min_count), key=lambda x: (-x[1], -len(x[0])))
    pruned: list[tuple[str, int]] = []
    for g, c in out:
        if not any(g in p and c == pc for p, pc in pruned):
            pruned.append((g, c))
    return pruned[:15]


def what_worked(rows: list[dict], top_quantile: float = 0.25) -> dict:
    """Compara el cuartil superior por rendimiento relativo con el resto, por formato."""
    scored = [r for r in rows if r.get("relative") is not None and r.get("title")]
    out: dict = {"method": f"top {int(top_quantile * 100)}% por 'relative' (views/día ÷ mediana de su cohorte) vs. resto",
                 "n_scored": len(scored), "by_format": {}, "title_formulas": _recurring_phrases([r["title"] for r in rows if r.get("title")]),
                 "caveats": ["Correlación, no causalidad.",
                             "Sin CTR ni retención (privados): la miniatura y el gancho se juzgan por revisión visual.",
                             "Visitas/día desde publicación es un promedio histórico."]}
    for fmt in sorted({r.get("format", "unknown") for r in scored}):
        group = sorted((r for r in scored if r.get("format", "unknown") == fmt), key=lambda r: -r["relative"])
        if len(group) < 6:
            out["by_format"][fmt] = {"n": len(group), "note": "muestra insuficiente (<6) para comparar"}
            continue
        k = max(2, round(len(group) * top_quantile))
        top, rest = group[:k], group[k:]
        feats = []
        for name, fn in _FEATURES.items():
            pt = sum(fn(r["title"]) for r in top) / len(top)
            pr = sum(fn(r["title"]) for r in rest) / len(rest)
            if abs(pt - pr) >= 0.15:
                feats.append({"rasgo": name, "top": round(pt, 2), "resto": round(pr, 2),
                              "evidencia": _strength(len(top), len(rest), pt - pr)})
        num = {}
        for label, get in (("palabras_titulo", lambda r: _title_words(r["title"])),
                           ("duracion_s", lambda r: r.get("duration_s"))):
            vt = [get(r) for r in top if get(r) is not None]
            vr = [get(r) for r in rest if get(r) is not None]
            if vt and vr:
                num[label] = {"mediana_top": median(vt), "mediana_resto": median(vr)}
        dur_top = [_bucket_duration(r.get("duration_s")) for r in top]
        days_top = [WEEKDAYS[datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")).weekday()]
                    for r in top if r.get("published_at")]
        out["by_format"][fmt] = {
            "n": len(group), "n_top": len(top),
            "top_videos": [{"title": r["title"], "relative": r["relative"], "views": r.get("views"),
                            "video_id": r.get("video_id")} for r in top[:10]],
            "bottom_videos": [{"title": r["title"], "relative": r["relative"], "video_id": r.get("video_id")}
                              for r in group[-5:]],
            "rasgos_de_titulo": feats,
            "numericos": num,
            "duracion_mas_comun_top": max(set(d for d in dur_top if d), key=dur_top.count) if any(dur_top) else None,
            "dia_publicacion_top": {d: days_top.count(d) for d in sorted(set(days_top))} if days_top else None,
        }
    return out


def topics_ranked(rows: list[dict], stopwords: set[str] | None = None, min_videos: int = 3) -> list[dict]:
    """Palabras temáticas de título ordenadas por rendimiento relativo mediano (≥ min_videos)."""
    stop = stopwords or {
        "de", "la", "el", "en", "y", "a", "los", "las", "un", "una", "que", "por", "con", "del", "se", "es", "tu",
        "the", "of", "and", "to", "in", "is", "your", "why", "how", "what", "a", "for", "on", "this", "it", "you",
        "qué", "por", "como", "cómo", "más", "mas", "su", "sus", "al", "lo", "explicado", "explained",
    }
    by: dict[str, list[float]] = {}
    for r in rows:
        if r.get("relative") is None or not r.get("title"):
            continue
        for w in set(re.findall(r"[a-záéíóúñü]{4,}", r["title"].lower())):
            if w not in stop:
                by.setdefault(w, []).append(r["relative"])
    ranked = [{"tema": w, "videos": len(v), "relative_mediano": round(median(v), 2)}
              for w, v in by.items() if len(v) >= min_videos]
    return sorted(ranked, key=lambda x: -x["relative_mediano"])[:25]
