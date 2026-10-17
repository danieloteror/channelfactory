"""Invocación de Clip Master como agente de recortes (proceso/repositorio independiente).

El comando sale SOLO de config/factory.yaml (`clip_master.command`, con marcadores
`{request}` y `{out_dir}`), nunca de contenido de terceros. El adaptador no escribe
en la configuración de Clip Master y siempre envía `allow_publish: false`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from ..models import ClipRequest, ClipResult
from ..orchestrator.worker import Blocked


class CliTransport:
    name = "cli"

    def __init__(self, command: list[str], cwd: str | None, timeout_s: int = 3600):
        self.command, self.cwd, self.timeout_s = command, cwd, timeout_s

    def check(self) -> None:
        if not self.command:
            raise Blocked("clip_master.command no configurado: indica cómo se invoca Clip Master")
        if self.cwd and not Path(self.cwd).expanduser().exists():
            raise Blocked(f"ruta de Clip Master inexistente: {self.cwd}")
        exe = self.command[0]
        if not (shutil.which(exe) or Path(exe).exists()):
            raise Blocked(f"ejecutable de Clip Master no encontrado: {exe}")

    def run(self, req: ClipRequest, work_dir: Path) -> ClipResult:
        self.check()
        if req.allow_publish:
            raise ValueError("allow_publish debe ser false")
        work_dir.mkdir(parents=True, exist_ok=True)
        req_path = work_dir / "clip_request.json"
        req_path.write_text(req.model_dump_json(indent=2))
        out_dir = work_dir / "out"
        out_dir.mkdir(exist_ok=True)
        cmd = [c.replace("{request}", str(req_path)).replace("{out_dir}", str(out_dir)) for c in self.command]
        log = work_dir / "clip_master.log"
        with log.open("w") as fh:
            r = subprocess.run(cmd, cwd=Path(self.cwd).expanduser() if self.cwd else None, stdout=fh,
                               stderr=subprocess.STDOUT, timeout=self.timeout_s)
        if r.returncode != 0:
            raise RuntimeError(f"Clip Master terminó con código {r.returncode}; ver {log}")
        result_path = out_dir / "clip_result.json"
        if not result_path.exists():
            raise RuntimeError(f"Clip Master no escribió {result_path}")
        return ClipResult.model_validate_json(result_path.read_text())


class HttpTransport:
    name = "http"

    def __init__(self, url: str, timeout_s: int = 3600, poll_s: float = 10.0):
        self.url, self.timeout_s, self.poll_s = url.rstrip("/"), timeout_s, poll_s

    def run(self, req: ClipRequest, work_dir: Path) -> ClipResult:
        body = req.model_dump_json().encode()
        r = urllib.request.Request(f"{self.url}/jobs", data=body, method="POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=60) as resp:  # noqa: S310 - URL de configuración
            job = json.loads(resp.read())
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            with urllib.request.urlopen(f"{self.url}/jobs/{job['job_id']}", timeout=60) as resp:  # noqa: S310
                st = json.loads(resp.read())
            if st.get("status") in ("ok", "partial", "none", "error"):
                return ClipResult.model_validate(st)
            time.sleep(self.poll_s)
        raise TimeoutError("Clip Master no terminó a tiempo")


def get_transport(cfg) -> CliTransport | HttpTransport:  # noqa: ANN001 - ClipMasterCfg
    if cfg.transport == "http" or (cfg.transport == "auto" and cfg.http_url):
        if not cfg.http_url:
            raise Blocked("clip_master.http_url no configurado")
        return HttpTransport(cfg.http_url, cfg.timeout_seconds)
    if cfg.path is None and not cfg.command:
        raise Blocked("Clip Master no configurado: falta clip_master.path/command (ruta o repo del propietario)")
    return CliTransport(cfg.command or [], cfg.path, cfg.timeout_seconds)
