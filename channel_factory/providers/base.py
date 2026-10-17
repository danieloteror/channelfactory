"""Interfaces pequeñas y versionadas de proveedores.

Cada proveedor declara capacidades, coste estimado y salud. «Desconocido» se
distingue de «no soportado»: `None` = no comprobado; `False` = no soportado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

INTERFACE_VERSION = "1.0"

HealthStatus = Literal["ok", "degraded", "blocked", "disabled", "unknown"]


@dataclass
class Health:
    status: HealthStatus
    detail: str = ""
    checks: dict[str, bool | None] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "degraded")


@dataclass
class VoiceInfo:
    voice_id: str
    language: str
    gender: str | None = None
    accent: str | None = None
    notes: str | None = None


@dataclass
class SynthResult:
    path: Path
    duration_s: float
    sample_rate: int
    voice_id: str
    timing_precision: Literal["word", "segment"] = "segment"
    word_timestamps: list[dict] | None = None  # solo si el motor los entrega o hay alineación válida
    cost_usd: float = 0.0


@runtime_checkable
class TTSProvider(Protocol):
    provider_id: str

    def health(self) -> Health: ...
    def voices(self) -> list[VoiceInfo]: ...
    def synthesize(self, text: str, voice_id: str, out_path: Path, speed: float = 1.0) -> SynthResult: ...
    def estimate_cost(self, text: str) -> float: ...


@dataclass
class ImageRequest:
    scene_id: str
    description: str
    width: int
    height: int
    character_refs: list[Path] = field(default_factory=list)
    style_refs: list[Path] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)


@runtime_checkable
class ImageProvider(Protocol):
    provider_id: str

    def health(self) -> Health: ...
    def generate(self, req: ImageRequest, out_path: Path) -> Path: ...
    def estimate_cost(self, req: ImageRequest) -> float: ...


@runtime_checkable
class LLMProvider(Protocol):
    provider_id: str

    def health(self) -> Health: ...
    def complete(self, system: str, prompt: str, max_tokens: int = 4000) -> str: ...
    def estimate_cost(self, prompt: str, max_tokens: int) -> float: ...
