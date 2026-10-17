"""Biblia de serie persistente por canal: identidad, personajes y referencias aprobadas.

La biblia vive en el perfil del canal (series_bible, world_rules, characters_dir) y no se
reconstruye en cada sesión. Las referencias visuales aprobadas se versionan en
`characters/<personaje>/approved/` y se pasan a los proveedores de imagen en cada escena.
"""

from __future__ import annotations

from pathlib import Path

import yaml

IDENTITY_CHECKS = [
    "especie/silueta del personaje",
    "colores y marcas distintivas",
    "ropa y accesorios fijos",
    "proporciones",
    "número de extremidades",
    "elementos característicos (orejas, cola, peinado…)",
]


def load(settings, channel_id: str) -> dict:  # noqa: ANN001
    prof = settings.channel(channel_id)
    out: dict = {"channel": channel_id, "bible": None, "world_rules": None, "characters": {}}
    for key, rel in (("bible", prof.series_bible), ("world_rules", prof.world_rules)):
        p = settings.path(rel)
        if p and p.exists():
            out[key] = p.read_text(encoding="utf-8")
    cdir = settings.path(prof.visual.characters_dir)
    if cdir and cdir.exists():
        for spec in sorted(cdir.glob("*.yaml")):
            data = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
            cid = data.get("id", spec.stem)
            approved = cdir / cid / "approved"
            data["approved_references"] = sorted(str(p) for p in approved.glob("*")) if approved.exists() else []
            out["characters"][cid] = data
    return out


def references_for(bible: dict, character_ids: list[str]) -> list[Path]:
    """Referencias aprobadas para condicionar la generación de una escena (no basta repetir prompt/seed)."""
    refs: list[Path] = []
    for cid in character_ids:
        refs += [Path(p) for p in bible["characters"].get(cid, {}).get("approved_references", [])]
    return refs
