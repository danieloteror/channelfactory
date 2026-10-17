"""Caché de artefactos por contenido/modelo/configuración e invalidación selectiva.

Cada etapa calcula un `cache_key` a partir de sus entradas (texto de la escena,
voz, modelo, parámetros). Si el artefacto ya existe y es válido, la etapa no se
repite. Un cambio de guion solo cambia las claves de las escenas afectadas, así
que el resto de artefactos se conserva.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..db import DB, new_id, utcnow_iso


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def cache_key(stage: str, inputs: Any) -> str:
    blob = json.dumps({"stage": stage, "inputs": inputs}, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


class Artifacts:
    def __init__(self, db: DB):
        self.db = db

    def lookup(self, stage: str, key: str) -> dict | None:
        r = self.db.one("SELECT * FROM artifacts WHERE stage=? AND cache_key=? AND valid=1", (stage, key))
        if not r:
            return None
        if not Path(r["path"]).exists():
            # El archivo desapareció: el artefacto ya no es válido.
            self.db.execute("UPDATE artifacts SET valid=0 WHERE artifact_id=?", (r["artifact_id"],))
            return None
        if r["sha256"] and Path(r["path"]).is_file() and sha256_file(r["path"]) != r["sha256"]:
            self.db.execute("UPDATE artifacts SET valid=0 WHERE artifact_id=?", (r["artifact_id"],))
            return None
        return dict(r)

    def record(self, stage: str, key: str, path: Path | str, *, kind: str, episode_id: str | None = None,
               inputs: Any = None, version: int = 1) -> dict:
        p = Path(path)
        digest = sha256_file(p) if p.is_file() else None
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO artifacts(artifact_id, episode_id, stage, kind, path, sha256, cache_key, version,
                       inputs_json, valid, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(stage, cache_key) DO UPDATE SET path=excluded.path, sha256=excluded.sha256,
                       valid=1, version=excluded.version, inputs_json=excluded.inputs_json""",
                (new_id("art"), episode_id, stage, kind, str(p), digest, key, version,
                 json.dumps(inputs, default=str), utcnow_iso()),
            )
        return self.lookup(stage, key)  # type: ignore[return-value]

    def invalidate_episode(self, episode_id: str, stages: list[str] | None = None) -> int:
        sql, params = "UPDATE artifacts SET valid=0 WHERE episode_id=?", [episode_id]
        if stages:
            sql += f" AND stage IN ({','.join('?' * len(stages))})"
            params += stages
        return self.db.execute(sql, tuple(params)).rowcount

    def protect(self, path: str, until_iso: str) -> None:
        """Marca un medio como necesario hasta una fecha (p. ej. publicación futura)."""
        self.db.execute(
            "UPDATE artifacts SET retain_until=MAX(COALESCE(retain_until,''), ?) WHERE path=?", (until_iso, path)
        )

    def deletable(self, now_iso: str | None = None) -> list[dict]:
        now_iso = now_iso or utcnow_iso()
        rows = self.db.query(
            """SELECT a.* FROM artifacts a
               WHERE (a.retain_until IS NULL OR a.retain_until < ?)
                 AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.media_path=a.path
                                 AND p.state NOT IN ('published','cancelled','failed'))""",
            (now_iso,),
        )
        return [dict(r) for r in rows]
