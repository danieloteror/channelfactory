"""Almacenamiento de medios con URL pública persistente (requisito de Metricool).

Un enlace que caduca antes de la fecha de publicación no sirve: se rechazan URLs
prefirmadas salvo que su caducidad supere la fecha programada con margen.
Las copias locales se conservan hasta confirmar la publicación.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


class StorageBlocked(RuntimeError):
    pass


@dataclass
class UrlCheck:
    ok: bool
    status: int | None
    content_type: str | None
    content_length: int | None
    problems: list[str]


_PRESIGN = re.compile(r"(X-Amz-Expires|X-Amz-Signature|Expires=|[?&]se=|[?&]sig=|token=)", re.I)


def presign_expiry(url: str) -> datetime | None:
    """Caducidad aproximada de URLs prefirmadas conocidas (S3 v4, S3 v2, Azure SAS)."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "X-Amz-Date" in q and "X-Amz-Expires" in q:
        start = datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return start + timedelta(seconds=int(q["X-Amz-Expires"][0]))
    if "Expires" in q and q["Expires"][0].isdigit():
        return datetime.fromtimestamp(int(q["Expires"][0]), tz=timezone.utc)
    if "se" in q:
        try:
            return datetime.fromisoformat(q["se"][0].replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def verify_public_url(url: str, *, expected_bytes: int | None = None, expected_type: str = "video/",
                      needed_until: datetime | None = None, margin: timedelta = timedelta(days=2),
                      timeout: float = 20.0) -> UrlCheck:
    problems: list[str] = []
    if not url.startswith("https://"):
        problems.append("la URL debe ser https")
    if _PRESIGN.search(url):
        exp = presign_expiry(url)
        if exp is None:
            problems.append("URL con firma/caducidad no interpretable: usa una URL pública persistente")
        elif needed_until is None or exp < needed_until + margin:
            problems.append(f"la URL caduca {exp.isoformat()} antes de lo necesario")
    status = ctype = clen = None
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URL validada arriba
            status = r.status
            ctype = r.headers.get("Content-Type")
            clen = int(r.headers["Content-Length"]) if r.headers.get("Content-Length") else None
    except urllib.error.HTTPError as e:
        status = e.code
        problems.append(f"HEAD devolvió {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        problems.append(f"no accesible: {e}")
    if status == 200:
        if not ctype or not ctype.startswith(expected_type):
            problems.append(f"Content-Type {ctype} no es {expected_type}*")
        if expected_bytes is not None and clen is not None and clen != expected_bytes:
            problems.append(f"tamaño remoto {clen} ≠ local {expected_bytes}")
        if clen is None:
            problems.append("sin Content-Length: no se puede verificar el tamaño")
    return UrlCheck(not problems, status, ctype, clen, problems)


class MediaStorage:
    name = "base"

    def publish(self, local: Path, key: str) -> str:  # devuelve URL pública
        raise NotImplementedError


class LocalOnlyStorage(MediaStorage):
    name = "local_only"

    def publish(self, local: Path, key: str) -> str:
        raise StorageBlocked(
            "No hay almacenamiento con URL pública configurado (publishing.media_storage). Metricool necesita una URL "
            "pública y persistente: configura un bucket público (S3/R2/Supabase) o Google Drive/Dropbox vinculado."
        )


class S3CompatiblePublicStorage(MediaStorage):
    """Bucket público (S3, R2, MinIO…). Variables: CF_S3_BUCKET, CF_S3_PUBLIC_BASE_URL, CF_S3_ENDPOINT (opcional)
    y credenciales estándar AWS_* del propietario. No usa URLs prefirmadas."""

    name = "s3_public"

    def __init__(self) -> None:
        self.bucket = os.environ.get("CF_S3_BUCKET")
        self.base_url = os.environ.get("CF_S3_PUBLIC_BASE_URL")
        self.endpoint = os.environ.get("CF_S3_ENDPOINT")
        if not (self.bucket and self.base_url):
            raise StorageBlocked("faltan CF_S3_BUCKET y/o CF_S3_PUBLIC_BASE_URL")

    def publish(self, local: Path, key: str) -> str:
        try:
            import boto3
        except ImportError as e:
            raise StorageBlocked("boto3 no instalado (pip install boto3)") from e
        s3 = boto3.client("s3", endpoint_url=self.endpoint) if self.endpoint else boto3.client("s3")
        ctype = "video/mp4" if local.suffix == ".mp4" else ("image/jpeg" if local.suffix in (".jpg", ".jpeg") else "image/png")
        s3.upload_file(str(local), self.bucket, key, ExtraArgs={"ContentType": ctype})
        return f"{self.base_url.rstrip('/')}/{urllib.parse.quote(key)}"


class SupabasePublicStorage(MediaStorage):
    """Bucket público de Supabase Storage. Variables: CF_SUPABASE_URL, CF_SUPABASE_SERVICE_KEY, CF_SUPABASE_BUCKET.
    Sin verificar contra un proyecto real."""

    name = "supabase_public"

    def __init__(self) -> None:
        self.url = os.environ.get("CF_SUPABASE_URL")
        self.key = os.environ.get("CF_SUPABASE_SERVICE_KEY")
        self.bucket = os.environ.get("CF_SUPABASE_BUCKET")
        if not (self.url and self.key and self.bucket):
            raise StorageBlocked("faltan CF_SUPABASE_URL / CF_SUPABASE_SERVICE_KEY / CF_SUPABASE_BUCKET")

    def publish(self, local: Path, key: str) -> str:
        ctype = "video/mp4" if local.suffix == ".mp4" else "image/jpeg"
        req = urllib.request.Request(
            f"{self.url.rstrip('/')}/storage/v1/object/{self.bucket}/{urllib.parse.quote(key)}",
            data=local.read_bytes(), method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": ctype, "x-upsert": "true"},
        )
        with urllib.request.urlopen(req, timeout=600) as r:  # noqa: S310
            if r.status not in (200, 201):
                raise RuntimeError(f"subida falló: {r.status}")
        return f"{self.url.rstrip('/')}/storage/v1/object/public/{self.bucket}/{urllib.parse.quote(key)}"


def get_storage(name: str | None) -> MediaStorage:
    if not name:
        return LocalOnlyStorage()
    return {"s3_public": S3CompatiblePublicStorage, "supabase_public": SupabasePublicStorage,
            "local_only": LocalOnlyStorage}[name]()
