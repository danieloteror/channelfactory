"""Capacidades normalizadas de cada transporte de publicación.

IMPORTANTE: estas claves son INTERNAS de Channel Factory, no campos de Metricool.
`None` = desconocido / no verificado; `False` = no soportado.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Capabilities:
    transport: str
    can_schedule_video: bool | None
    can_poll_status: bool | None
    can_set_thumbnail: bool | None
    can_set_language: bool | None
    unattended: bool | None  # puede operar sin una sesión interactiva
    verified: bool  # probado contra la cuenta real
    supported_networks: list[str] = field(default_factory=list)
    max_duration: dict[str, float | None] = field(default_factory=dict)  # segundos por tipo de post
    max_bytes: dict[str, int | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# Límites documentados por Metricool / plataforma a 2026-10 (revalidar).
# Shorts: límite conservador del proyecto; TikTok vía Metricool: 3 s–10 min, ≤ 500 MB.
DOC_LIMITS = {
    "youtube_video": {"max_duration": None, "max_bytes": None, "min_duration": 1.0},
    "youtube_short": {"max_duration": 175.0, "max_bytes": None, "min_duration": 1.0},
    "tiktok_video": {"max_duration": 600.0, "max_bytes": 500 * 1024 * 1024, "min_duration": 3.0},
}


def mcp_handoff() -> Capabilities:
    return Capabilities(
        transport="mcp_handoff",
        can_schedule_video=True,  # createScheduledPost acepta video por URL pública
        can_poll_status=None,  # getScheduledPosts lista programados; no confirma publicación en la red
        can_set_thumbnail=True,  # videoThumbnailUrl (condiciones por red)
        can_set_language=None,  # sin campo documentado de idioma de audio/metadatos
        unattended=False,  # las herramientas MCP viven en la sesión interactiva, no en el worker
        verified=False,
        supported_networks=["youtube", "tiktok"],
        max_duration={k: v["max_duration"] for k, v in DOC_LIMITS.items()},
        max_bytes={k: v["max_bytes"] for k, v in DOC_LIMITS.items()},
        notes=[
            "El worker escribe una solicitud en var/outbox/metricool; la sesión de Claude la ejecuta con el MCP "
            "oficial y registra el resultado con `publish record-result`.",
            "No hay autonomía 24/7 con este transporte: requiere una sesión que procese la bandeja.",
        ],
    )


def http_api() -> Capabilities:
    return Capabilities(
        transport="http_api",
        can_schedule_video=True,
        can_poll_status=None,
        can_set_thumbnail=None,
        can_set_language=None,
        unattended=True,
        verified=False,
        supported_networks=["youtube", "tiktok"],
        max_duration={k: v["max_duration"] for k, v in DOC_LIMITS.items()},
        max_bytes={k: v["max_bytes"] for k, v in DOC_LIMITS.items()},
        notes=[
            "Requiere plan Advanced/Custom, token de usuario (X-Mc-Auth), userId/blogId y acceso de red a "
            "app.metricool.com. Sin verificar contra la cuenta real hasta una prueba de borrador.",
        ],
    )


def for_transport(name: str) -> Capabilities:
    return {"mcp_handoff": mcp_handoff, "http_api": http_api}[name]()
