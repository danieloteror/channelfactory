"""Carga y validación de configuración global y perfiles de canal."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PublishMode = Literal["preview", "draft", "autopilot"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCfg(_Strict):
    name: str = "channel-factory"
    timezone: str = "America/New_York"
    conversation_language: str = "es"
    state_dir: str = "var"
    models_dir: str = "~/.cache/channel_factory/models"


class RuntimeCfg(_Strict):
    heavy_jobs_concurrency: int = Field(1, ge=1)
    render_workers: int = Field(2, ge=1)  # procesos para renderizar escenas de un episodio en paralelo
    resume_enabled: bool = True
    lease_seconds: int = Field(900, ge=30)
    heartbeat_seconds: int = Field(30, ge=1)
    max_attempts: int = Field(3, ge=1)
    retry_backoff_seconds: list[int] = [30, 120, 600]


class LongVideoCfg(_Strict):
    width: int = 1920
    height: int = 1080
    fps: int = 30
    target_seconds: int = 480
    range_seconds: tuple[int, int] = (360, 600)
    words_per_minute: tuple[int, int] = (130, 165)


class ClipsCfg(_Strict):
    width: int = 1080
    height: int = 1920
    preferred_seconds: tuple[int, int] = (65, 120)
    youtube_max_seconds: int = 175
    max_per_episode: int = 3
    allow_shorter_if_better: bool = True
    reuse_existing_assets: bool = True


class AudioCfg(_Strict):
    integrated_lufs: float = -14.0
    true_peak_dbtp: float = -1.5
    music_bed_db: float = -22.0
    ducking: bool = True


class MonthlyCfg(_Strict):
    long_videos: int = Field(12, ge=0, le=60)
    shorts_per_long_max: int = Field(3, ge=0)
    recurring_enabled: bool = False


class DefaultsCfg(_Strict):
    output_language: str = "en-US"
    long_video: LongVideoCfg = LongVideoCfg()
    clips: ClipsCfg = ClipsCfg()
    audio: AudioCfg = AudioCfg()
    monthly: MonthlyCfg = MonthlyCfg()


class ProvidersCfg(_Strict):
    voice_preference: str = "kokoro"
    voice_backend: str = "auto"
    image_provider: str = "auto"
    animation_provider: str = "optional"
    render_backend: str = "ffmpeg"
    llm_provider: str = "session"


class ClipMasterCfg(_Strict):
    path: str | None = None
    transport: Literal["auto", "cli", "http"] = "auto"
    profile: str = "original_explainer"
    allow_publish: bool = False
    command: list[str] | None = None
    http_url: str | None = None
    timeout_seconds: int = 3600

    @field_validator("allow_publish")
    @classmethod
    def _single_owner(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "clip_master.allow_publish debe ser false: Channel Factory es el único "
                "propietario de la publicación de estos trabajos."
            )
        return v


class PublishingCfg(_Strict):
    # Desactivada por defecto: la integración con Metricool existe pero no se usa hasta activarla
    # (ver docs/metricool.md). Con enabled=false nada se envía y los episodios terminan listos para subir a mano.
    enabled: bool = False
    provider: Literal["metricool"] = "metricool"
    transport: Literal["auto", "mcp_handoff", "http_api"] = "auto"
    mode: PublishMode = "preview"
    youtube_long: bool = True
    youtube_shorts: bool = True
    tiktok_clips: bool = True
    tiktok_full_episode: bool = False
    require_verified_destination: bool = True
    media_storage: str | None = None


class BudgetCfg(_Strict):
    currency: str = "USD"
    paid_providers_enabled: bool = False
    episode_cap_usd: float | None = None
    batch_cap_usd: float | None = None
    monthly_cap_usd: float | None = None
    max_paid_retries_per_asset: int = 1


class ChannelRef(_Strict):
    enabled: bool = False
    profile: str


class FactoryConfig(_Strict):
    project: ProjectCfg = ProjectCfg()
    runtime: RuntimeCfg = RuntimeCfg()
    defaults: DefaultsCfg = DefaultsCfg()
    providers: ProvidersCfg = ProvidersCfg()
    clip_master: ClipMasterCfg = ClipMasterCfg()
    publishing: PublishingCfg = PublishingCfg()
    budget: BudgetCfg = BudgetCfg()
    channels: dict[str, ChannelRef] = {}
    protected_brand_ids: list[str] = []  # marcas de otros flujos: nunca se publica en ellas


# ---------------------------------------------------------------- canal


class ReferenceChannel(_Strict):
    url: str
    channel_id: str | None = None
    role: str = "reference"
    language: str | None = None


class VoiceProfile(_Strict):
    provider: str = "kokoro"
    selected_voice: str | None = None  # elección definitiva del propietario
    provisional_voice: str | None = None  # para pilotos mientras no se elige
    speed: float = Field(1.0, gt=0.5, lt=1.6)
    narration_mode: Literal["external_narrator", "protagonist", "undecided"] = "undecided"
    lexicon: str | None = None
    audition_text: str | None = None  # texto propio del canal para audiciones
    audition_candidates: list[str] = []  # ids de voz o mezclas `blend:a*0.6+b*0.4`
    format_samples: dict[str, str] = {}  # {narrator_external: texto, protagonist_first_person: texto}

    @property
    def effective_voice(self) -> str | None:
        return self.selected_voice or self.provisional_voice


class VisualProfile(_Strict):
    provider: str = "auto"
    protagonist: str | None = None
    protagonist_proposal_selected: str | None = None
    characters_dir: str | None = None
    brand_dir: str | None = None


class ChannelPublishing(_Strict):
    mode: PublishMode = "preview"
    timezone: str = "America/New_York"
    metricool_brand_id: str | None = None
    metricool_brand_label: str | None = None
    youtube_destination_id: str | None = None
    tiktok_destination_id: str | None = None
    made_for_kids: bool = False
    synthetic_content_disclosure: bool = True
    youtube_category: str = "EDUCATION"


class ChannelProfile(_Strict):
    id: str
    name: str
    enabled: bool = False
    niche: str
    language: str = "en-US"
    audience: str | None = None
    editorial_promise: str | None = None
    series_bible: str | None = None
    format_bible: str | None = None
    world_rules: str | None = None
    reference_channels: list[ReferenceChannel] = []
    voice: VoiceProfile = VoiceProfile()
    visual: VisualProfile = VisualProfile()
    publishing: ChannelPublishing = ChannelPublishing()
    monthly: MonthlyCfg | None = None
    budget: BudgetCfg | None = None
    banned_topics: list[str] = []
    published_topics_file: str | None = None

    @model_validator(mode="after")
    def _autopilot_needs_destinations(self) -> "ChannelProfile":
        if self.publishing.mode == "autopilot":
            missing = [
                k
                for k in ("metricool_brand_id", "youtube_destination_id", "tiktok_destination_id")
                if not getattr(self.publishing, k)
            ]
            if missing:
                raise ValueError(
                    f"Canal {self.id}: autopilot requiere destinos verificados; faltan {missing}"
                )
        return self


# ---------------------------------------------------------------- carga


def find_root(start: Path | None = None) -> Path:
    env = os.environ.get("CHANNEL_FACTORY_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    p = (start or Path.cwd()).resolve()
    for cand in (p, *p.parents):
        if (cand / "config" / "factory.yaml").exists():
            return cand
    # Paquete instalado en modo editable: la raíz es el padre del paquete.
    return Path(__file__).resolve().parent.parent


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} no contiene un mapa YAML")
    return data


class Settings:
    """Configuración resuelta: global + perfiles de canal, con rutas absolutas."""

    def __init__(self, root: Path, cfg: FactoryConfig):
        self.root = root
        self.cfg = cfg
        self._channels: dict[str, ChannelProfile] = {}

    @property
    def state_dir(self) -> Path:
        p = Path(self.cfg.project.state_dir).expanduser()
        p = p if p.is_absolute() else self.root / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def models_dir(self) -> Path:
        p = Path(os.environ.get("CHANNEL_FACTORY_MODELS_DIR", self.cfg.project.models_dir)).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.state_dir / "factory.sqlite"

    def path(self, rel: str | None) -> Path | None:
        if rel is None:
            return None
        p = Path(rel).expanduser()
        return p if p.is_absolute() else self.root / p

    def channel(self, channel_id: str) -> ChannelProfile:
        if channel_id not in self._channels:
            ref = self.cfg.channels.get(channel_id)
            if ref is None:
                raise KeyError(f"Canal desconocido: {channel_id}. Definidos: {sorted(self.cfg.channels)}")
            prof_path = self.path(ref.profile)
            if not prof_path or not prof_path.exists():
                raise FileNotFoundError(f"Perfil de canal no encontrado: {prof_path}")
            prof = ChannelProfile.model_validate(_read_yaml(prof_path))
            if prof.id != channel_id:
                raise ValueError(f"{prof_path}: id '{prof.id}' no coincide con '{channel_id}'")
            self._channels[channel_id] = prof
        return self._channels[channel_id]

    def channel_dir(self, channel_id: str) -> Path:
        ref = self.cfg.channels[channel_id]
        return self.path(ref.profile).parent  # type: ignore[union-attr]

    def enabled_channels(self) -> list[str]:
        return [cid for cid, ref in self.cfg.channels.items() if ref.enabled]

    @property
    def publishing_enabled(self) -> bool:
        return self.cfg.publishing.enabled

    def effective_publish_mode(self, channel_id: str) -> PublishMode:
        """El modo más restrictivo entre el global y el del canal; `preview` si la publicación está desactivada."""
        if not self.cfg.publishing.enabled:
            return "preview"
        order = {"preview": 0, "draft": 1, "autopilot": 2}
        g = self.cfg.publishing.mode
        c = self.channel(channel_id).publishing.mode
        return g if order[g] <= order[c] else c

    def monthly(self, channel_id: str) -> MonthlyCfg:
        return self.channel(channel_id).monthly or self.cfg.defaults.monthly

    def budget(self, channel_id: str | None = None) -> BudgetCfg:
        if channel_id:
            b = self.channel(channel_id).budget
            if b:
                return b
        return self.cfg.budget


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@lru_cache(maxsize=4)
def _load(root: str) -> Settings:
    """Lee config/factory.yaml y, si existe, config/factory.local.yaml (fuera de Git) encima."""
    r = Path(root)
    data = _read_yaml(r / "config" / "factory.yaml")
    local = r / "config" / "factory.local.yaml"
    if local.exists():
        data = _deep_merge(data, _read_yaml(local))
    return Settings(r, FactoryConfig.model_validate(data))


def load_settings(root: Path | None = None) -> Settings:
    return _load(str(find_root(root)))


def settings_from(root: Path, cfg: FactoryConfig) -> Settings:
    """Construye Settings sin leer disco (pruebas)."""
    return Settings(root, cfg)
