"""Construcción del objeto `info` de Metricool (compartido por MCP y API HTTP).

Los campos siguen la definición de la herramienta MCP `createScheduledPost` de Metricool:
providers, publicationDate {dateTime, timezone}, text, media (URLs públicas),
autoPublish, draft, youtubeData / tiktokData solo para las redes incluidas.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

POST_TYPES = {"youtube_video": ("youtube", "video"), "youtube_short": ("youtube", "short"), "tiktok_video": ("tiktok", None)}


def build_info(*, post_type: str, text: str, title: str, media_url: str, scheduled_local: datetime, timezone: str,
               mode: str, made_for_kids: bool, ai_disclosure: bool, category: str = "EDUCATION",
               tags: list[str] | None = None, privacy: str = "public", thumbnail_url: str | None = None) -> dict:
    """Devuelve `info` listo para createScheduledPost / POST /api/v2/scheduler/posts."""
    if post_type not in POST_TYPES:
        raise ValueError(f"tipo de post desconocido: {post_type}")
    if mode == "preview":
        raise ValueError("modo preview: no se construyen envíos")
    network, yt_type = POST_TYPES[post_type]
    local = scheduled_local if scheduled_local.tzinfo else scheduled_local.replace(tzinfo=ZoneInfo(timezone))
    info: dict = {
        "autoPublish": mode == "autopilot",
        "draft": mode == "draft",
        "descendants": [],
        "firstCommentText": "",
        "hasNotReadNotes": False,
        "media": [media_url],
        "mediaAltText": [],
        "providers": [{"network": network}],
        "publicationDate": {"dateTime": local.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%dT%H:%M:%S"),
                            "timezone": timezone},
        "shortener": False,
        "smartLinkData": {"ids": []},
        "text": text,
    }
    if thumbnail_url:
        info["videoThumbnailUrl"] = thumbnail_url
    if network == "youtube":
        info["youtubeData"] = {
            "title": title, "type": yt_type, "privacy": privacy, "tags": tags or [], "category": category,
            "madeForKids": bool(made_for_kids), "isAiGeneratedContent": bool(ai_disclosure),
        }
    else:
        info["tiktokData"] = {
            "disableComment": False, "disableDuet": False, "disableStitch": False,
            "privacyOption": "PUBLIC_TO_EVERYONE" if privacy == "public" else "SELF_ONLY",
            "commercialContentThirdParty": False, "commercialContentOwnBrand": False,
            "title": title, "autoAddMusic": False, "isAigc": bool(ai_disclosure),
        }
    return info
