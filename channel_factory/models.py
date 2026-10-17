"""Modelos de dominio versionados (episodios, escenas, claims, clips, publicaciones)."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


class EpisodeState(str, Enum):
    planned = "planned"
    researching = "researching"
    script_ready = "script_ready"
    assets_generating = "assets_generating"
    audio_ready = "audio_ready"
    rendering = "rendering"
    qa = "qa"
    clipping = "clipping"
    ready_to_schedule = "ready_to_schedule"
    scheduled = "scheduled"
    published = "published"
    failed = "failed"
    blocked = "blocked"
    cancelled = "cancelled"


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    blocked = "blocked"
    cancelled = "cancelled"


class ErrorClass(str, Enum):
    transient = "transient"  # reintentable con backoff
    permanent = "permanent"  # error de datos/código: no reintentar
    blocked = "blocked"  # falta acceso, modelo, cuota o decisión humana


class PublicationState(str, Enum):
    draft = "draft"  # creado localmente
    validated = "validated"  # pasó validación previa
    ready = "ready"  # listo para enviar (medio accesible)
    submitting = "submitting"  # lock tomado; envío en curso
    handoff = "handoff"  # solicitud escrita para la sesión interactiva (MCP); pendiente de resultado
    unconfirmed = "unconfirmed"  # timeout/respuesta ambigua: conciliar antes de reintentar
    scheduled = "scheduled"  # confirmado en el gestor (no significa publicado)
    published = "published"  # confirmado en la red
    failed = "failed"
    blocked = "blocked"
    cancelled = "cancelled"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(_M):
    claim_id: str
    text: str
    kind: Literal["stable_principle", "current_data", "historical_fact", "definition", "fictional_example", "assumption"]
    sources: list[dict] = []  # {title, url, publisher, accessed, quote?}
    as_of: str | None = None
    certainty: Literal["high", "medium", "low"] = "medium"
    scene_ids: list[str] = []
    notes: str | None = None


class SceneVisual(_M):
    """Capas de una escena. Coordenadas normalizadas sobre el lienzo (0–1).

    Capas (`characters`, `props`): {image, x, y, height, z, flip, enter:{at|at_text, anim, dur},
    exit:{at|at_text, anim, dur}, swaps:[{at|at_text, image}], idle: none|bob, id}.
    Cámara: {from: [cx, cy, zoom], to: [cx, cy, zoom], ease}.
    Overlays (pantalla, no lienzo): {type: text|counter|bar_chart|line_chart, at|at_text, until, ...}.
    """

    background: str | None = None  # ruta de imagen relativa al episodio o color "#rrggbb"
    video: dict | None = None  # fondo en video (p. ej. salida de una skill de estilo): {path, start_s, loop, style}
    canvas: tuple[int, int] | None = None  # por defecto el tamaño del fondo o 3840x2160
    characters: list[dict] = []
    props: list[dict] = []
    camera: dict = {}
    overlays: list[dict] = []
    vertical_roi: tuple[float, float, float, float] | None = None  # x,y,w,h normalizados
    vertical_layout: dict | None = None  # recomposición 9:16: {layers: {id: {x,y,height}}, overlays: [...]}
    prompt: str | None = None  # descripción para el proveedor de imagen
    references: list[str] = []  # referencias visuales aprobadas (personaje, estilo)


class Scene(_M):
    scene_id: str
    text: str
    beat: Literal[
        "hook", "question", "setup", "mechanism", "example", "twist", "nuance", "resolution", "cta", "transition"
    ]
    claims: list[str] = []
    visual: SceneVisual = SceneVisual()
    sfx: list[dict] = []  # {file, at|at_text, gain_db, license}
    transition_in: Literal["cut", "crossfade", "fade_black"] = "cut"
    lead_in_s: float = 0.15
    lead_out_s: float = 0.35
    hold_s: float | None = None  # duración fija si la escena no tiene narración
    duration_s: float | None = None  # duración REAL de la narración medida del audio
    audio_path: str | None = None
    status: Literal["pending", "audio_ready", "rendered", "failed"] = "pending"
    cost_usd: float = 0.0
    clip_hint: Literal["question", "surprise", "example", "resolution"] | None = None


class EpisodeManifest(_M):
    schema_version: str = SCHEMA_VERSION
    channel_id: str
    episode_id: str
    title: str
    working_title: str | None = None
    language: str = "en-US"
    script_version: int = 1
    scenes: list[Scene]
    claims_file: str | None = None
    music: dict | None = None  # {path, license, source, gain_db?}
    thumbnail: dict | None = None  # {scene_id|image, title, family, colors}
    description: str | None = None
    tags: list[str] = []


class ClipRequest(_M):
    """Contrato v1.0 hacia Clip Master (ver docs/clip_master_contract.md)."""

    schema_version: str = SCHEMA_VERSION
    job_id: str
    channel_id: str
    episode_id: str
    source_video: str
    source_checksum: str
    source_type: Literal["original_explainer"] = "original_explainer"
    language: str = "en-US"
    transcript_path: str
    scene_manifest_path: str
    preferred_duration_seconds: tuple[int, int] = (65, 120)
    max_duration_seconds: int = 175
    max_clips: int = Field(3, ge=0, le=10)
    aspect_ratio: Literal["9:16"] = "9:16"
    destination_profiles: list[str] = ["youtube_shorts", "tiktok"]
    allow_publish: Literal[False] = False
    candidate_hints: list[dict] = []  # segmentos sugeridos por la estructura del guion


class ClipScores(_M):
    hook: float
    standalone: float
    payoff: float
    visual_legibility: float
    overall: float


class ClipOutput(_M):
    clip_id: str
    file: str
    checksum: str
    source_start_s: float
    source_end_s: float
    script_version: int
    transcript: str
    hook: str
    rationale: str
    scores: ClipScores
    subtitles: dict  # {srt: path, vtt: path}
    thumbnail: str | None = None
    quality_report: dict


class ClipResult(_M):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    status: Literal["ok", "partial", "none", "error"]
    clips: list[ClipOutput] = []
    explanation: str | None = None  # obligatorio si se entregan menos clips de los pedidos
