"""claims.json: afirmación, fuente, fecha, certeza y escenas relacionadas.

Reglas: todo dato actual necesita fuente y fecha (`as_of`); los ejemplos ficticios y
supuestos deben estar marcados como tales; cada claim citado en una escena existe;
cifras con % o unidades se comprueban en el texto de la escena.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import Claim, EpisodeManifest

NEEDS_SOURCE = {"current_data", "historical_fact", "stable_principle", "definition"}


def load(path: Path) -> list[Claim]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("claims", data) if isinstance(data, dict) else data
    return [Claim.model_validate(c) for c in items]


def check(claims: list[Claim], manifest: EpisodeManifest) -> list[str]:
    problems = []
    ids = {c.claim_id for c in claims}
    if len(ids) != len(claims):
        problems.append("claim_id duplicados")
    scene_ids = {s.scene_id for s in manifest.scenes}
    for c in claims:
        if c.kind in NEEDS_SOURCE and not c.sources:
            problems.append(f"{c.claim_id}: '{c.kind}' sin fuente")
        for src in c.sources:
            if not src.get("url") or not src.get("accessed"):
                problems.append(f"{c.claim_id}: fuente sin url o fecha de consulta")
        if c.kind == "current_data" and not c.as_of:
            problems.append(f"{c.claim_id}: dato actual sin as_of")
        for sid in c.scene_ids:
            if sid not in scene_ids:
                problems.append(f"{c.claim_id}: escena inexistente {sid}")
    for s in manifest.scenes:
        for cid in s.claims:
            if cid not in ids:
                problems.append(f"{s.scene_id}: cita claim inexistente {cid}")
        # Cifras con porcentaje en la narración deben estar respaldadas por algún claim de la escena.
        if re.search(r"\d+(\.\d+)?\s?%", s.text) and not s.claims:
            problems.append(f"{s.scene_id}: contiene porcentajes sin claim asociado")
    return problems


def numbers_in(text: str) -> list[str]:
    return re.findall(r"\d[\d,]*(?:\.\d+)?\s?%?", text)
