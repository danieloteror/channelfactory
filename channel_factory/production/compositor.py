"""Compositor por escena: fondo + capas + cámara + overlays → MP4 (solo video).

- Cámara con extensión en coma flotante (`Image.transform(EXTENT)`): zooms y paneos
  suaves sin el temblor típico de `zoompan`.
- Capas con entrada/salida (pop, fade, slide), cambios de imagen (expresiones) en un
  instante o al aparecer una palabra (`at_text`) y respiración sutil (`idle: bob`).
- Overlays de pantalla con tipografía real y gráficos desde datos (graphics.py).
- Modo vertical 9:16 a partir del mismo lienzo: recorte por `vertical_roi` y, si una
  capa esencial quedaría cortada, recomposición con `vertical_layout` (mismos assets).
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from ..models import Scene
from .graphics import OVERLAYS, ease, rgba

DEFAULT_CANVAS = (3840, 2160)


# ---------------------------------------------------------------- tiempos


def text_time(scene: Scene, needle: str, narration_s: float) -> float:
    """Instante aproximado en que la voz dice `needle` (proporcional a caracteres).

    Precisión de segmento: sin alineación de palabras no se inventan timestamps
    exactos; para cortes finos usar alineación (WhisperX) cuando esté disponible.
    """
    text = re.sub(r"\[pause [\d.]+\]", "", scene.text)
    idx = text.lower().find(needle.lower())
    if idx < 0:
        raise ValueError(f"{scene.scene_id}: at_text '{needle}' no aparece en el texto de la escena")
    return scene.lead_in_s + narration_s * (idx / max(len(text), 1))


def resolve_at(scene: Scene, spec: dict | None, narration_s: float, default: float | None = 0.0) -> float | None:
    if not spec:
        return default
    if spec.get("at_text"):
        return text_time(scene, spec["at_text"], narration_s) + float(spec.get("offset", 0.0))
    if spec.get("at") is not None:
        return float(spec["at"])
    return default


def scene_duration(scene: Scene) -> float:
    if scene.duration_s:
        return scene.lead_in_s + scene.duration_s + scene.lead_out_s
    if scene.hold_s:
        return scene.hold_s
    raise ValueError(f"{scene.scene_id}: sin duración de narración ni hold_s")


# ---------------------------------------------------------------- capas


@dataclass
class Layer:
    lid: str
    images: list[tuple[float, Image.Image]]  # (desde_t, imagen) ordenado
    x: float
    y: float
    height: float
    z: int
    flip: bool
    t_in: float
    anim_in: str
    dur_in: float
    t_out: float | None
    anim_out: str
    dur_out: float
    idle: str
    essential: bool = True
    _cache: dict = field(default_factory=dict)

    def image_at(self, t: float) -> Image.Image:
        cur = self.images[0][1]
        for start, img in self.images:
            if t >= start:
                cur = img
        return cur


def _load_rgba(path: Path) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img.convert("RGBA")


def _build_layers(scene: Scene, base: Path, narration_s: float, overrides: dict | None) -> list[Layer]:
    out = []
    specs = list(scene.visual.characters) + list(scene.visual.props)
    for i, spec in enumerate(specs):
        lid = spec.get("id", f"layer{i}")
        spec = {**spec, **((overrides or {}).get(lid, {}))}
        if spec.get("hidden"):
            continue
        imgs = [(-1.0, _load_rgba(base / spec["image"]))]
        for sw in spec.get("swaps", []):
            imgs.append((resolve_at(scene, sw, narration_s), _load_rgba(base / sw["image"])))
        imgs.sort(key=lambda x: x[0])
        enter, exit_ = spec.get("enter", {}), spec.get("exit")
        out.append(Layer(
            lid=lid, images=imgs, x=spec.get("x", 0.5), y=spec.get("y", 0.6), height=spec.get("height", 0.6),
            z=spec.get("z", 10 + i), flip=bool(spec.get("flip", False)),
            t_in=resolve_at(scene, enter, narration_s) or 0.0, anim_in=enter.get("anim", "none"),
            dur_in=float(enter.get("dur", 0.35)),
            t_out=resolve_at(scene, exit_, narration_s, None) if exit_ else None,
            anim_out=(exit_ or {}).get("anim", "fade"), dur_out=float((exit_ or {}).get("dur", 0.3)),
            idle=spec.get("idle", "none"), essential=spec.get("essential", True),
        ))
    out.sort(key=lambda l: l.z)
    return out


def _layer_state(layer: Layer, t: float) -> tuple[float, float, float, float] | None:
    """(alpha, escala, dx, dy) relativos a la animación; None si no visible."""
    if t < layer.t_in:
        return None
    alpha, sc, dx, dy = 1.0, 1.0, 0.0, 0.0
    p = (t - layer.t_in) / max(layer.dur_in, 1e-3)
    if p < 1:
        e = ease(p, "out")
        if layer.anim_in == "fade":
            alpha = e
        elif layer.anim_in == "pop":
            alpha = min(1.0, p * 3)
            sc = 0.6 + 0.4 * e + 0.08 * math.sin(e * math.pi)
        elif layer.anim_in == "slide_left":
            dx, alpha = 0.35 * (1 - e), min(1.0, p * 2)
        elif layer.anim_in == "slide_right":
            dx, alpha = -0.35 * (1 - e), min(1.0, p * 2)
        elif layer.anim_in == "rise":
            dy, alpha = 0.25 * (1 - e), min(1.0, p * 2)
    if layer.t_out is not None and t >= layer.t_out:
        q = (t - layer.t_out) / max(layer.dur_out, 1e-3)
        if q >= 1:
            return None
        alpha *= 1 - ease(q, "in")
    if layer.idle == "bob":
        dy += 0.004 * math.sin(t * 2 * math.pi / 3.2)
        sc *= 1 + 0.004 * math.sin(t * 2 * math.pi / 3.2 + 1.1)
    return alpha, sc, dx, dy


# ---------------------------------------------------------------- cámara


def camera_rect(cam: dict, t: float, dur: float, canvas: tuple[int, int], out_aspect: float,
                roi: tuple[float, float, float, float] | None = None) -> tuple[float, float, float, float]:
    """Rectángulo del lienzo visible (x0, y0, x1, y1) en el instante t."""
    cw, ch = canvas
    if roi:  # vertical: la ROI define el encuadre base
        bx, by, bw, bh = roi[0] * cw, roi[1] * ch, roi[2] * cw, roi[3] * ch
        base_cx, base_cy = bx + bw / 2, by + bh / 2
        base_h = min(bh, ch)
        base_w = base_h * out_aspect
        if base_w > cw:
            base_w, base_h = cw, cw / out_aspect
    else:
        base_cx, base_cy = cw / 2, ch / 2
        base_w = min(cw, ch * out_aspect)
        base_h = base_w / out_aspect
    a = cam.get("from", [0.5, 0.5, 1.0])
    b = cam.get("to", a)
    k = ease(t / max(dur, 1e-3), cam.get("ease", "in_out"))
    cx = (a[0] + (b[0] - a[0]) * k) if not roi else None
    cy = (a[1] + (b[1] - a[1]) * k) if not roi else None
    zoom = a[2] + (b[2] - a[2]) * k
    w, h = base_w / zoom, base_h / zoom
    if roi:
        # En vertical la cámara solo aplica el zoom relativo alrededor del centro de la ROI.
        x_c, y_c = base_cx, base_cy
    else:
        x_c, y_c = cx * cw, cy * ch
    x0 = min(max(x_c - w / 2, 0), cw - w)
    y0 = min(max(y_c - h / 2, 0), ch - h)
    return (x0, y0, x0 + w, y0 + h)


# ---------------------------------------------------------------- render


@dataclass
class RenderReport:
    path: str
    frames: int
    duration_s: float
    width: int
    height: int
    clipped_layers: list[str] = field(default_factory=list)
    recomposed: bool = False


def _background(scene: Scene, base: Path) -> Image.Image:
    bg = scene.visual.background or "#000000"
    canvas = tuple(scene.visual.canvas) if scene.visual.canvas else None
    if bg.startswith("#"):
        return Image.new("RGB", canvas or DEFAULT_CANVAS, rgba(bg)[:3])
    img = Image.open(base / bg).convert("RGB")
    if canvas and img.size != canvas:
        img = img.resize(canvas, Image.LANCZOS)
    return img


def layer_box(layer: Layer, canvas: tuple[int, int], img: Image.Image, sc: float = 1.0, dx: float = 0.0,
              dy: float = 0.0) -> tuple[float, float, float, float]:
    cw, ch = canvas
    h = layer.height * ch * sc
    w = h * img.width / img.height
    cx, cy = (layer.x + dx) * cw, (layer.y + dy) * ch
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def clipped_essentials(layers: list[Layer], canvas: tuple[int, int], rect: tuple[float, float, float, float],
                       tol: float = 0.04) -> list[str]:
    """Capas esenciales que quedarían cortadas por el encuadre (más de `tol` de su ancho/alto)."""
    x0, y0, x1, y1 = rect
    bad = []
    for l in layers:
        if not l.essential:
            continue
        bx0, by0, bx1, by1 = layer_box(l, canvas, l.images[0][1])
        bw, bh = bx1 - bx0, by1 - by0
        if bx0 < x0 - tol * bw or bx1 > x1 + tol * bw or by0 < y0 - tol * bh or by1 > y1 + tol * bh:
            bad.append(l.lid)
    return bad


class VideoSource:
    """Fotogramas de un MP4 (fondo de escena) al tamaño del lienzo, en flujo secuencial.

    Lectura secuencial eficiente; un salto hacia atrás o adelante reabre FFmpeg con -ss.
    `loop`: repite el clip; si no, mantiene el último fotograma cuando se acaba.
    """

    def __init__(self, path: Path, size: tuple[int, int], fps: int, start_s: float = 0.0, loop: bool = True):
        self.path, self.size, self.fps, self.start, self.loop = Path(path), size, fps, start_s, loop
        from ..quality.technical import ffprobe

        self.duration = float(ffprobe(self.path)["format"].get("duration", 0)) - start_s
        self._proc = None
        self._next_i = None
        self._last: Image.Image | None = None

    def _open(self, t: float) -> None:
        self.close()
        w, h = self.size
        self._proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-ss", f"{self.start + t:.3f}", "-i", str(self.path), "-an", "-vf",
             f"fps={self.fps},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
             "-f", "rawvideo", "-pix_fmt", "rgba", "-"], stdout=subprocess.PIPE)
        self._next_i = int(round(t * self.fps))

    def frame(self, t: float) -> Image.Image:
        if self.duration > 0 and t >= self.duration:
            t = (t % self.duration) if self.loop else max(self.duration - 1 / self.fps, 0)
        i = int(round(t * self.fps))
        if self._proc is None or i != self._next_i:
            self._open(i / self.fps)
        w, h = self.size
        buf = self._proc.stdout.read(w * h * 4)
        if len(buf) < w * h * 4:
            if self._last is None:
                raise RuntimeError(f"no se pudo leer el video {self.path}")
            return self._last
        self._next_i = i + 1
        self._last = Image.frombuffer("RGBA", (w, h), buf, "raw", "RGBA", 0, 1)
        return self._last

    def close(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
            self._proc = None


class SceneRenderer:
    """Prepara una escena una vez y produce cualquier fotograma `frame(t)` (RGBA).

    El fondo se preescala a la resolución de trabajo necesaria para el zoom máximo, y la
    cámara se aplica con OpenCV (warpAffine) si está instalado, o con Pillow (resize con caja
    en coma flotante) si no: ambos dan movimiento subpíxel sin temblor.
    """

    def __init__(self, scene: Scene, episode_dir: Path, *, width: int, height: int, vertical: bool = False,
                 fps: int = 30):
        self.scene, self.width, self.height, self.vertical = scene, width, height, vertical
        base = Path(episode_dir)
        self.narration_s = scene.duration_s or 0.0
        self.dur = scene_duration(scene)
        bg = _background(scene, base)
        aspect = width / height
        vid = scene.visual.video
        vl = scene.visual.vertical_layout or {}
        cam = scene.visual.camera or {}
        zmax = max(cam.get("from", [0, 0, 1.0])[2], cam.get("to", cam.get("from", [0, 0, 1.0]))[2], 1.0)
        base_w = min(bg.width, bg.height * aspect)
        k = min(1.0, 1.1 * width * zmax / base_w)
        if k < 0.98:
            bg = bg.resize((max(2, round(bg.width * k)), max(2, round(bg.height * k))), Image.LANCZOS)
        self.canvas = bg.size
        self.bg = bg.convert("RGBA")
        self.layers = _build_layers(scene, base, self.narration_s, None)
        roi = tuple(scene.visual.vertical_roi) if (vertical and scene.visual.vertical_roi) else None
        if vertical and roi is None:
            cw, ch = self.canvas
            roi = (0.5 - (ch * aspect / cw) / 2, 0.0, ch * aspect / cw, 1.0)
        self.roi = roi
        self.report = RenderReport("", 0, self.dur, width, height)
        overlays = [dict(o) for o in scene.visual.overlays]
        if vertical:
            probe = camera_rect(cam, 0, self.dur, self.canvas, aspect, roi)
            clipped = clipped_essentials(self.layers, self.canvas, probe)
            self.report.clipped_layers = clipped
            if clipped and vl:
                self.layers = _build_layers(scene, base, self.narration_s, vl.get("layers"))
                if vl.get("roi"):
                    self.roi = tuple(vl["roi"])
                self.report.recomposed = True
                self.report.clipped_layers = clipped_essentials(
                    self.layers, self.canvas, camera_rect(cam, 0, self.dur, self.canvas, aspect, self.roi))
            if "overlays" in vl:
                overlays = [dict(o) for o in vl["overlays"]]
        for ov in overlays:
            ov["_t0"] = resolve_at(scene, ov, self.narration_s, 0.0)
            until = ov.get("until")
            ov["_t1"] = float(until) if until is not None else (
                text_time(scene, ov["until_text"], self.narration_s) if ov.get("until_text") else self.dur)
        self.overlays = overlays
        self.scale = height / 1080 if not vertical else width / 1080
        self.video = VideoSource(base / vid["path"], self.canvas, fps,
                                 float(vid.get("start_s", 0.0)), bool(vid.get("loop", True))) if vid else None
        try:
            import cv2  # noqa: F401
            import numpy as np

            self._bg_np = np.asarray(self.bg)
        except ImportError:
            self._bg_np = None

    def _camera(self, rect: tuple[float, float, float, float], t: float = 0.0) -> Image.Image:
        src_img = self.video.frame(t) if self.video is not None else self.bg
        if self._bg_np is not None:
            import cv2
            import numpy as np

            src = np.asarray(src_img) if self.video is not None else self._bg_np
            sx = self.width / (rect[2] - rect[0])
            m = np.float32([[sx, 0, -rect[0] * sx], [0, sx, -rect[1] * sx]])
            out = cv2.warpAffine(src, m, (self.width, self.height), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE)
            return Image.fromarray(out, "RGBA")
        return src_img.resize((self.width, self.height), Image.BILINEAR, box=rect)

    def frame(self, t: float, prev: Image.Image | None = None) -> Image.Image:
        rect = camera_rect(self.scene.visual.camera, t, self.dur, self.canvas, self.width / self.height, self.roi)
        frame = self._camera(rect, t)
        sx = self.width / (rect[2] - rect[0])
        for l in self.layers:
            st = _layer_state(l, t)
            if st is None:
                continue
            alpha, sc, dx, dy = st
            img = l.image_at(t)
            bx0, by0, bx1, by1 = layer_box(l, self.canvas, img, sc, dx, dy)
            dw, dh = int(round((bx1 - bx0) * sx)), int(round((by1 - by0) * sx))
            if dw < 2 or dh < 2:
                continue
            key = (id(img), dw, dh, l.flip)
            piece = l._cache.get(key)
            if piece is None:
                piece = img.transpose(Image.FLIP_LEFT_RIGHT) if l.flip else img
                piece = piece.resize((dw, dh), Image.BILINEAR)
                if len(l._cache) > 12:
                    l._cache.clear()
                l._cache[key] = piece
            if alpha < 0.999:
                piece = piece.copy()
                piece.putalpha(piece.getchannel("A").point(lambda v, a=alpha: int(v * a)))
            _paste_clipped(frame, piece, int(round((bx0 - rect[0]) * sx)), int(round((by0 - rect[1]) * sx)))
        for ov in self.overlays:
            if ov["_t0"] <= t < ov["_t1"]:
                kind = ov.get("type", "text")
                elapsed = t - ov["_t0"]  # segundos desde la aparición
                if kind == "text":
                    OVERLAYS[kind](frame, ov, elapsed, self.scale)
                else:
                    OVERLAYS[kind](frame, ov, elapsed, self.scale, elapsed)
        if prev is not None and self.scene.transition_in == "crossfade" and t < 0.4:
            frame = Image.blend(prev, frame, ease(t / 0.4))
        if self.scene.transition_in == "fade_black" and t < 0.3:
            frame = Image.blend(Image.new("RGBA", frame.size, (0, 0, 0, 255)), frame, ease(t / 0.3))
        return frame


def last_frame(scene: Scene, episode_dir: Path, *, width: int, height: int, fps: int,
               vertical: bool = False) -> Image.Image:
    """Último fotograma de una escena sin renderizarla entera (para fundidos encadenados en paralelo)."""
    r = SceneRenderer(scene, episode_dir, width=width, height=height, vertical=vertical, fps=fps)
    n = max(1, int(round(r.dur * fps)))
    try:
        return r.frame((n - 1) / fps)
    finally:
        if r.video is not None:
            r.video.close()


def render_scene(scene: Scene, episode_dir: Path, out_path: Path, *, width: int, height: int, fps: int,
                 prev_scene: Scene | None = None, vertical: bool = False, crf: int = 18,
                 preset: str = "veryfast") -> RenderReport:
    r = SceneRenderer(scene, episode_dir, width=width, height=height, vertical=vertical, fps=fps)
    prev = None
    if prev_scene is not None and scene.transition_in == "crossfade":
        prev = last_frame(prev_scene, episode_dir, width=width, height=height, fps=fps, vertical=vertical)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}",
         "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-g", str(fps * 2), "-movflags", "+faststart", str(out_path)],
        stdin=subprocess.PIPE,
    )
    n = max(1, int(round(r.dur * fps)))
    last = None
    try:
        for i in range(n):
            last = r.frame(i / fps, prev)
            ff.stdin.write(last.tobytes())
    finally:
        ff.stdin.close()
        rc = ff.wait()
        if r.video is not None:
            r.video.close()
    if rc != 0:
        raise RuntimeError(f"ffmpeg falló renderizando {scene.scene_id} (código {rc})")
    if last is not None:
        last.convert("RGB").save(out_path.with_suffix(".last.png"))
    report = r.report
    report.path, report.frames = str(out_path), n
    return report


def _paste_clipped(frame: Image.Image, piece: Image.Image, x: int, y: int) -> None:
    """alpha_composite exige que la pieza quepa: recortamos la parte visible."""
    l, t = max(0, -x), max(0, -y)
    r, b = min(piece.width, frame.width - x), min(piece.height, frame.height - y)
    if r <= l or b <= t:
        return
    frame.alpha_composite(piece.crop((l, t, r, b)), (x + l, y + t))
