"""Diagnóstico: hardware, herramientas, modelos, red, credenciales (solo presencia) y adaptadores."""

from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request

from .production.voices import hardware_report
from .providers.registry import builtin_providers, check_user_tool, load_user_tools

HOSTS = {
    "youtube.com (yt-dlp, Shorts)": "https://www.youtube.com",
    "googleapis.com (YouTube Data API)": "https://www.googleapis.com/youtube/v3/",
    "github.com (modelos Kokoro/ASR)": "https://github.com",
    "huggingface.co (modelos opcionales)": "https://huggingface.co",
    "pypi.org": "https://pypi.org/simple/",
}

ENV_VARS = ["YOUTUBE_API_KEY", "METRICOOL_USER_TOKEN", "METRICOOL_USER_ID", "ANTHROPIC_API_KEY", "BFL_API_KEY",
            "ELEVENLABS_API_KEY", "CF_S3_BUCKET", "CF_S3_PUBLIC_BASE_URL", "CF_SUPABASE_URL"]


def reach(url: str, timeout: float = 8.0) -> str:
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout)  # noqa: S310
        return "ok"
    except urllib.error.HTTPError as e:  # respondió el servidor: el host es accesible
        return f"ok (HTTP {e.code})"
    except (urllib.error.URLError, OSError) as e:
        return f"bloqueado/sin acceso ({getattr(e, 'reason', e)})"


def run(settings, network: bool = True) -> dict:  # noqa: ANN001
    out: dict = {"hardware": hardware_report()}
    out["tools"] = {t: bool(shutil.which(t)) for t in ("ffmpeg", "ffprobe", "fc-match", "yt-dlp", "git", "node")}
    out["providers"] = {k: {"status": v.status, "detail": v.detail} for k, v in builtin_providers(settings).items()}
    from .quality.asr import WhisperASR

    out["providers"]["asr:whisper_base_en"] = (
        {"status": "ok", "detail": "sherpa-onnx whisper base.en"} if WhisperASR(settings.models_dir).available()
        else {"status": "blocked", "detail": "modelo ASR ausente: `channel-factory models fetch-asr`"})
    for lib in ("scenedetect", "cv2", "librosa", "silero_vad", "audio_separator"):
        try:
            __import__(lib)
            out["providers"][f"analysis:{lib}"] = {"status": "ok", "detail": "instalado"}
        except ImportError:
            out["providers"][f"analysis:{lib}"] = {"status": "disabled", "detail": "opcional, no instalado"}
    out["user_tools"] = {t.id: check_user_tool(t, settings.root).__dict__
                         for t in load_user_tools(settings.root / "providers" / "user_tools.yaml")}
    out["env"] = {k: bool(os.environ.get(k)) for k in ENV_VARS}  # solo presencia, nunca valores
    cm = settings.cfg.clip_master
    out["clip_master"] = {"configured": bool(cm.path or cm.command or cm.http_url), "transport": cm.transport,
                          "allow_publish": cm.allow_publish}
    out["publishing"] = {"enabled": settings.publishing_enabled, "provider": "metricool (opcional)",
                         "transport": settings.cfg.publishing.transport,
                         "global_mode": settings.cfg.publishing.mode,
                         "media_storage": settings.cfg.publishing.media_storage}
    chans = {}
    for cid, ref in settings.cfg.channels.items():
        try:
            p = settings.channel(cid)
            chans[cid] = {"enabled": ref.enabled, "mode": settings.effective_publish_mode(cid),
                          "voice": p.voice.effective_voice, "brand": p.publishing.metricool_brand_id,
                          "youtube": p.publishing.youtube_destination_id, "tiktok": p.publishing.tiktok_destination_id}
        except Exception as e:  # noqa: BLE001
            chans[cid] = {"error": str(e)}
    out["channels"] = chans
    b = settings.cfg.budget
    out["budget"] = {"paid_enabled": b.paid_providers_enabled, "episode_cap": b.episode_cap_usd,
                     "batch_cap": b.batch_cap_usd, "monthly_cap": b.monthly_cap_usd}
    if network:
        out["network"] = {name: reach(url) for name, url in HOSTS.items()}
    out["blockers"] = blockers(out)
    return out


def blockers(d: dict) -> list[str]:
    b = []
    if not d["tools"]["ffmpeg"]:
        b.append("Instala FFmpeg (render y QA).")
    if d["providers"]["tts:kokoro_onnx"]["status"] != "ok":
        b.append("TTS: " + d["providers"]["tts:kokoro_onnx"]["detail"])
    if not d["env"]["YOUTUBE_API_KEY"]:
        b.append("Análisis de canales: falta YOUTUBE_API_KEY.")
    if not d["clip_master"]["configured"]:
        b.append("Clip Master: falta clip_master.path/command (ruta del proyecto y cómo se invoca).")
    if d["publishing"]["enabled"]:
        if not d["publishing"]["media_storage"]:
            b.append("Metricool: falta almacenamiento con URL pública persistente para los medios.")
        if not d["env"]["METRICOOL_USER_TOKEN"]:
            b.append("Metricool sin API: la publicación usa la bandeja para la sesión (MCP), no es desatendida.")
    net = d.get("network", {})
    for name, st in net.items():
        if not st.startswith("ok"):
            b.append(f"Red: {name} → {st}")
    return b
