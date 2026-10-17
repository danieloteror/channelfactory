"""Transportes hacia Metricool.

- `McpHandoffTransport`: el worker no puede llamar al MCP (vive en la sesión de Claude).
  Escribe en la bandeja de salida la llamada exacta (`createScheduledPost` + argumentos);
  la sesión la ejecuta y registra el resultado con `publish record-result`.
- `HttpApiTransport`: API REST (plan Advanced/Custom). Autenticación `X-Mc-Auth` + userId/blogId.
  Implementada según la documentación pública; SIN verificar contra la cuenta real.
Nunca se ponen credenciales en URLs ni en logs.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class TransportBlocked(RuntimeError):
    """Falta plan, credencial o acceso de red: bloqueo real."""


class TransportPermanent(RuntimeError):
    """Rechazo definitivo (4xx de validación): no reintentar sin cambiar el contenido."""


class TransportAmbiguous(RuntimeError):
    """Se envió pero no hay confirmación (timeout/5xx tras enviar): conciliar antes de reintentar."""


@dataclass
class SendResult:
    kind: str  # "scheduled" | "handoff"
    remote_id: str | None = None
    planner_url: str | None = None
    response: dict | None = None
    outbox_path: str | None = None


class McpHandoffTransport:
    name = "mcp_handoff"

    def __init__(self, outbox_dir: Path):
        self.outbox = Path(outbox_dir)

    def send(self, pub_id: str, blog_id: str, date_iso: str, info: dict) -> SendResult:
        self.outbox.mkdir(parents=True, exist_ok=True)
        req = {
            "pub_id": pub_id,
            "server": "Metricool (MCP oficial)",
            "tool": "createScheduledPost",
            "arguments": {"blogId": str(blog_id), "date": date_iso, "info": json.dumps(info)},
            "after": f"channel-factory publish record-result {pub_id} <respuesta.json>",
            "on_error_or_timeout": "no reintentar a ciegas: ejecutar getScheduledPosts y `publish reconcile`",
        }
        path = self.outbox / f"{pub_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(req, indent=2))
        tmp.replace(path)
        return SendResult("handoff", outbox_path=str(path))


class HttpApiTransport:
    name = "http_api"
    BASE = "https://app.metricool.com/api/v2/scheduler/posts"

    def __init__(self, timeout: float = 60.0):
        self.token = os.environ.get("METRICOOL_USER_TOKEN")
        self.user_id = os.environ.get("METRICOOL_USER_ID")
        self.timeout = timeout

    def check(self) -> None:
        if not self.token or not self.user_id:
            raise TransportBlocked("faltan METRICOOL_USER_TOKEN / METRICOOL_USER_ID (API requiere plan Advanced/Custom)")

    def send(self, pub_id: str, blog_id: str, date_iso: str, info: dict) -> SendResult:
        self.check()
        url = f"{self.BASE}?{urllib.parse.urlencode({'userId': self.user_id, 'blogId': blog_id})}"
        body = json.dumps(info).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"X-Mc-Auth": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                data = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode(errors="replace")
            if e.code in (401, 403):
                raise TransportBlocked(f"Metricool {e.code}: credenciales/plan sin acceso") from e
            if e.code == 429:
                raise TransportBlocked("Metricool 429: cuota agotada; reintentar más tarde") from e
            if 400 <= e.code < 500:
                raise TransportPermanent(f"Metricool {e.code}: {detail}") from e
            raise TransportAmbiguous(f"Metricool {e.code} tras enviar: {detail}") from e
        except (TimeoutError, socket.timeout) as e:
            raise TransportAmbiguous(f"timeout tras enviar: {e}") from e
        except urllib.error.URLError as e:
            # Si la conexión no llegó a establecerse no hubo envío: es un bloqueo de red, no ambigüedad.
            if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)) or "403" in str(e.reason):
                raise TransportBlocked(f"sin acceso de red a Metricool: {e.reason}") from e
            raise TransportAmbiguous(f"error de red tras enviar: {e.reason}") from e
        post = data.get("data", data) if isinstance(data, dict) else {}
        return SendResult("scheduled", remote_id=str(post.get("id")) if post.get("id") else None,
                          planner_url=post.get("plannerUrl"), response=data)


def get_transport(name: str, state_dir: Path):  # noqa: ANN201
    if name in ("auto", "mcp_handoff"):
        if name == "auto" and os.environ.get("METRICOOL_USER_TOKEN"):
            return HttpApiTransport()
        return McpHandoffTransport(state_dir / "outbox" / "metricool")
    if name == "http_api":
        return HttpApiTransport()
    raise ValueError(f"transporte desconocido: {name}")
