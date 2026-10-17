"""Cliente mínimo de YouTube Data API v3 (metadatos y estadísticas públicas).

Requiere `YOUTUBE_API_KEY`. Valores ocultos o no disponibles quedan en `None`
(nunca 0 inventado). Las métricas privadas (CTR, retención, ingresos) no existen
aquí: requieren YouTube Analytics con autorización del propietario.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://www.googleapis.com/youtube/v3"


class ApiBlocked(RuntimeError):
    pass


def _get(path: str, **params) -> dict:  # noqa: ANN003
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise ApiBlocked("falta YOUTUBE_API_KEY (YouTube Data API v3)")
    params["key"] = key
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - host fijo
        return json.loads(r.read())


def iso_duration(s: str | None) -> float | None:
    if not s:
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _int(x) -> int | None:  # noqa: ANN001
    return int(x) if x is not None else None


def resolve_channel(url_or_handle: str) -> dict:
    """Acepta URL de canal (@handle, /channel/UC…) o handle; devuelve identidad + uploads playlist."""
    s = url_or_handle.strip()
    m = re.search(r"/channel/(UC[\w-]{22})", s)
    params = {"part": "snippet,statistics,contentDetails"}
    if m:
        params["id"] = m.group(1)
    else:
        h = re.search(r"@([\w.\-]+)", s)
        if not h:
            raise ValueError(f"no reconozco el canal: {s}")
        params["forHandle"] = "@" + h.group(1)
    data = _get("channels", **params)
    if not data.get("items"):
        raise LookupError(f"canal no encontrado: {s}")
    it = data["items"][0]
    st = it.get("statistics", {})
    return {
        "channel_id": it["id"], "title": it["snippet"]["title"], "handle": it["snippet"].get("customUrl"),
        "published_at": it["snippet"].get("publishedAt"), "country": it["snippet"].get("country"),
        "description": it["snippet"].get("description"),
        "subscribers": None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
        "views": _int(st.get("viewCount")), "videos": _int(st.get("videoCount")),
        "uploads_playlist": it["contentDetails"]["relatedPlaylists"]["uploads"],
        "source": "youtube_data_api_v3", "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def list_uploads(playlist_id: str, max_items: int = 500) -> list[str]:
    ids, token = [], None
    while len(ids) < max_items:
        kw = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if token:
            kw["pageToken"] = token
        data = _get("playlistItems", **kw)
        ids += [x["contentDetails"]["videoId"] for x in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return ids[:max_items]


def videos(ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 50):
        data = _get("videos", part="snippet,statistics,contentDetails,liveStreamingDetails", id=",".join(ids[i:i + 50]))
        for it in data.get("items", []):
            st, sn = it.get("statistics", {}), it["snippet"]
            out.append({
                "video_id": it["id"], "title": sn["title"], "published_at": sn["publishedAt"],
                "duration_s": iso_duration(it["contentDetails"].get("duration")),
                "views": _int(st.get("viewCount")), "likes": _int(st.get("likeCount")),
                "comments": _int(st.get("commentCount")),
                "thumbnail": (sn.get("thumbnails", {}).get("maxres") or sn.get("thumbnails", {}).get("high") or {}).get("url"),
                "is_live": bool(it.get("liveStreamingDetails")) or sn.get("liveBroadcastContent") in ("live", "upcoming"),
                "tags": sn.get("tags", []), "description": sn.get("description"),
            })
    return out


def classify_format(v: dict, shorts_probe: bool | None = None) -> tuple[str, str]:
    """(formato, método). Sin bandera oficial de Shorts: la duración sola no basta."""
    if v.get("is_live"):
        return "live", "liveStreamingDetails"
    if shorts_probe is True:
        return "short", "shorts_url_probe"
    if shorts_probe is False:
        return "long", "shorts_url_probe"
    d = v.get("duration_s")
    if d is None:
        return "unknown", "sin duración"
    if d > 180:
        return "long", "duración > 180 s"
    return "short_candidate", "duración ≤ 180 s sin confirmar formato"


def probe_short(video_id: str, timeout: float = 15.0) -> bool | None:
    """youtube.com/shorts/<id> responde 200 si es Short y redirige si no. None si no hay acceso."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            return None

    try:
        opener = urllib.request.build_opener(NoRedirect)
        r = opener.open(urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}", method="HEAD"),
                        timeout=timeout)
        return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (301, 302, 303, 307, 308) else None
    except (urllib.error.URLError, OSError):
        return None
