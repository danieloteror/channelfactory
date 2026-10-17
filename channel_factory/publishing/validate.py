"""Validación previa: bloquea ANTES de enviar si destino, modo, medio o metadatos no son correctos."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..orchestrator.artifacts import sha256_file
from ..quality.technical import ffprobe
from .capabilities import DOC_LIMITS
from .storage import verify_public_url

TITLE_MAX = {"youtube": 100, "tiktok": 2200}
TEXT_MAX = {"youtube": 5000, "tiktok": 2200}
NETWORK_KEY = {"youtube": "youtubeData", "tiktok": "tiktokData"}


@dataclass
class Validation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_brand_snapshot(path: Path) -> list[dict]:
    """Instantánea de getBrandSettings guardada por la sesión interactiva (`publish brands-refresh`)."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data.get("data", data) if isinstance(data, dict) else data


def check_destination(*, brand_id: str | None, network: str, profile_brand_id: str | None,
                      protected_brand_ids: list[str], brands: list[dict],
                      destination_id: str | None) -> list[str]:
    problems = []
    if not profile_brand_id:
        problems.append("el perfil del canal no tiene metricool_brand_id")
    if brand_id != profile_brand_id:
        problems.append(f"marca {brand_id} ≠ marca del perfil {profile_brand_id}")
    if brand_id in {str(b) for b in protected_brand_ids}:
        problems.append(f"marca {brand_id} protegida (pertenece a otro flujo): nunca se publica en ella")
    if not destination_id:
        problems.append(f"sin destino {network} verificado en el perfil del canal")
    brand = next((b for b in brands if str(b.get("id")) == str(brand_id)), None)
    if brand is None:
        problems.append("marca no encontrada en la instantánea de Metricool (refresca con `publish brands-refresh`)")
    else:
        connected = (brand.get("networksData") or {}).get(NETWORK_KEY.get(network, ""))
        if not connected:
            problems.append(f"la marca {brand.get('label')} no tiene {network} conectado en Metricool")
        elif destination_id and str(connected) != str(destination_id):
            problems.append(f"{network} conectado ({connected}) ≠ destino del perfil ({destination_id})")
    return problems


def check_media(path: Path | None, post_type: str, expected_sha: str | None) -> list[str]:
    if path is None or not Path(path).exists():
        return ["sin archivo de video"]
    problems = []
    if expected_sha and sha256_file(path) != expected_sha:
        problems.append("checksum del medio no coincide con el registrado")
    info = ffprobe(Path(path))
    v = [s for s in info["streams"] if s["codec_type"] == "video"]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not v:
        problems.append("el medio no tiene video")
    if not a:
        problems.append("el medio no tiene audio")
    dur = float(info["format"].get("duration", 0))
    size = int(info["format"].get("size", 0))
    lim = DOC_LIMITS[post_type]
    if dur < lim["min_duration"]:
        problems.append(f"duración {dur:.1f}s < mínimo {lim['min_duration']}s")
    if lim["max_duration"] and dur > lim["max_duration"]:
        problems.append(f"duración {dur:.1f}s > máximo {lim['max_duration']}s para {post_type}")
    if lim["max_bytes"] and size > lim["max_bytes"]:
        problems.append(f"tamaño {size} > {lim['max_bytes']} bytes")
    if v:
        w, h = v[0]["width"], v[0]["height"]
        vertical = h > w
        if post_type in ("youtube_short", "tiktok_video") and not vertical:
            problems.append(f"{post_type} requiere vertical (9:16); el medio es {w}x{h}")
        if post_type == "youtube_video" and vertical:
            problems.append(f"video largo horizontal esperado; el medio es {w}x{h}")
    return problems


def validate(pub, *, profile, protected_brand_ids: list[str], brands: list[dict], mode: str,  # noqa: ANN001
             check_url: bool = True, now: datetime | None = None) -> Validation:
    """`pub`: Publication del ledger; `profile`: ChannelProfile; `mode`: modo efectivo del canal."""
    v = Validation(ok=True)
    content = pub.content
    if mode == "preview":
        v.problems.append("modo preview: no se envía nada a Metricool")
    v.problems += check_destination(
        brand_id=pub.brand_id, network=pub.network, profile_brand_id=profile.publishing.metricool_brand_id,
        protected_brand_ids=protected_brand_ids, brands=brands,
        destination_id=profile.publishing.youtube_destination_id if pub.network == "youtube"
        else profile.publishing.tiktok_destination_id,
    )
    if pub.destination_id and pub.network == "youtube" and pub.destination_id != profile.publishing.youtube_destination_id:
        v.problems.append("destino de YouTube del registro ≠ perfil")
    v.problems += check_media(Path(pub.media_path) if pub.media_path else None, pub.post_type, pub.media_sha256)
    title, text = content.get("title"), content.get("text")
    if not title:
        v.problems.append("falta título")
    elif len(title) > TITLE_MAX[pub.network]:
        v.problems.append(f"título de {len(title)} caracteres > {TITLE_MAX[pub.network]}")
    if not text:
        v.problems.append("falta descripción/texto")
    elif len(text) > TEXT_MAX[pub.network]:
        v.problems.append(f"texto de {len(text)} caracteres > {TEXT_MAX[pub.network]}")
    if content.get("made_for_kids") is None:
        v.problems.append("audiencia (madeForKids) sin definir")
    try:
        when = datetime.fromisoformat(pub.scheduled_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo(pub.timezone))
        if when <= (now or datetime.now(timezone.utc)):
            v.problems.append("la fecha programada está en el pasado")
    except (TypeError, ValueError):
        v.problems.append(f"fecha programada inválida: {pub.scheduled_at}")
        when = None
    if check_url:
        if not pub.media_url:
            v.problems.append("sin URL pública del medio")
        else:
            size = Path(pub.media_path).stat().st_size if pub.media_path and Path(pub.media_path).exists() else None
            chk = verify_public_url(pub.media_url, expected_bytes=size, needed_until=when)
            v.problems += [f"URL: {p}" for p in chk.problems]
    if content.get("thumbnail_url") and pub.network == "tiktok":
        v.warnings.append("la miniatura de TikTok solo aplica a cuentas Business")
    v.ok = not v.problems
    return v
