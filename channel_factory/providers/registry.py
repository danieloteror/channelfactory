"""Registro de proveedores: integrados + herramientas del usuario (providers/user_tools.yaml)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from .base import Health

CAPABILITIES = {
    "tts", "image", "image_edit", "image_to_video", "text_to_video", "render", "asr", "alignment",
    "vad", "stem_separation", "llm", "storage", "publishing", "clip_extraction",
}


class UserTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    enabled: bool = False
    capabilities: list[str]
    transport: Literal["cli", "http", "python", "comfyui_workflow"]
    executable: str | None = None
    working_directory: str | None = None
    endpoint: str | None = None
    credentials_env: list[str] = []
    input_schema_path: str | None = None
    output_schema_path: str | None = None
    license_notes: str | None = None
    max_concurrency: int = 1
    cost_estimator: str | float | None = None

    @field_validator("capabilities")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = set(v) - CAPABILITIES
        if unknown:
            raise ValueError(f"capacidades desconocidas: {sorted(unknown)}")
        return v


def load_user_tools(path: Path) -> list[UserTool]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [UserTool.model_validate(t) for t in data.get("tools", [])]


def check_user_tool(tool: UserTool, root: Path) -> Health:
    """Comprueba lo verificable sin ejecutar la herramienta."""
    if not tool.enabled:
        return Health("disabled", "enabled=false")
    checks: dict[str, bool | None] = {}
    problems = []
    if tool.transport == "cli":
        exe = tool.executable and (shutil.which(tool.executable) or (Path(tool.executable).exists() and tool.executable))
        checks["executable"] = bool(exe)
        if not exe:
            problems.append(f"ejecutable no encontrado: {tool.executable}")
    if tool.transport in ("http", "comfyui_workflow"):
        checks["endpoint"] = bool(tool.endpoint)
        if not tool.endpoint:
            problems.append("falta endpoint")
    for var in tool.credentials_env:
        ok = bool(os.environ.get(var))
        checks[f"env:{var}"] = ok
        if not ok:
            problems.append(f"falta variable de entorno {var}")
    for label, rel in (("input_schema", tool.input_schema_path), ("output_schema", tool.output_schema_path)):
        if rel is None:
            checks[label] = None
            problems.append(f"{label} no definido: no se puede validar el contrato")
        else:
            p = Path(rel) if Path(rel).is_absolute() else root / rel
            checks[label] = p.exists()
            if not p.exists():
                problems.append(f"{label} no existe: {p}")
    if tool.cost_estimator is None:
        problems.append("sin estimador de coste: se tratará como de pago y bloqueado sin presupuesto")
    if problems:
        return Health("blocked", "; ".join(problems), checks)
    return Health("ok", "configuración verificable correcta (falta prueba de humo)", checks)


def builtin_providers(settings) -> dict:  # noqa: ANN001
    """Proveedores integrados y su salud real."""
    from .tts_kokoro import KokoroTTS

    out: dict[str, Health] = {}
    out["tts:kokoro_onnx"] = KokoroTTS(settings.models_dir).health()
    out["graphics:pillow"] = Health("ok", "tarjetas, textos y gráficas desde datos (Pillow + fontconfig)")
    try:
        import cairosvg  # noqa: F401

        out["graphics:svg"] = Health("ok", "rasterizado SVG (cairosvg)")
    except Exception as e:  # noqa: BLE001
        out["graphics:svg"] = Health("disabled", f"cairosvg no disponible: {e}")
    out["image:generative"] = Health(
        "disabled", "sin proveedor de imagen configurado: registra uno en providers/user_tools.yaml o con API key")
    out["render:ffmpeg"] = (
        Health("ok", shutil.which("ffmpeg") or "") if shutil.which("ffmpeg") and shutil.which("ffprobe")
        else Health("blocked", "ffmpeg/ffprobe no instalados")
    )
    out["llm:anthropic_api"] = (
        Health("ok", "ANTHROPIC_API_KEY presente (requiere presupuesto para usarse)")
        if os.environ.get("ANTHROPIC_API_KEY")
        else Health("disabled", "sin ANTHROPIC_API_KEY: los guiones los escribe el agente en sesión")
    )
    out["image:flux_api"] = (
        Health("ok", "BFL_API_KEY presente") if os.environ.get("BFL_API_KEY")
        else Health("disabled", "sin BFL_API_KEY")
    )
    out["tts:elevenlabs"] = (
        Health("ok", "ELEVENLABS_API_KEY presente") if os.environ.get("ELEVENLABS_API_KEY")
        else Health("disabled", "sin ELEVENLABS_API_KEY")
    )
    return out
