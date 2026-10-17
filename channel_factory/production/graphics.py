"""Gráficos exactos con tipografía real: textos, contadores y gráficas desde datos.

Nada de cifras o ejes inventados por un modelo generativo: todo se dibuja con
Pillow a partir de los valores del manifiesto.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont


@lru_cache(maxsize=64)
def font_path(family: str, style: str = "Bold") -> str:
    """Resuelve una familia con fontconfig (fc-match); devuelve la ruta del archivo."""
    p = Path(family)
    if p.suffix.lower() in (".ttf", ".otf") and p.exists():
        return str(p)
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", f"{family}:style={style}"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "DejaVuSans-Bold.ttf"


@lru_cache(maxsize=256)
def font(family: str, size: int, style: str = "Bold") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(family, style), max(int(size), 6))


def rgba(color: str | tuple, alpha: float = 1.0) -> tuple[int, int, int, int]:
    if isinstance(color, tuple):
        r, g, b = color[:3]
        a = color[3] if len(color) > 3 else 255
    else:
        r, g, b, *rest = ImageColor.getrgb(color) + (255,)
        a = rest[0] if rest else 255
    return (r, g, b, int(a * max(0.0, min(1.0, alpha))))


def ease(x: float, kind: str = "in_out") -> float:
    x = max(0.0, min(1.0, x))
    if kind == "linear":
        return x
    if kind == "out":
        return 1 - (1 - x) ** 3
    if kind == "in":
        return x**3
    return 3 * x * x - 2 * x * x * x  # smoothstep


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_text(img: Image.Image, ov: dict, progress: float, scale: float) -> None:
    """Texto en pantalla (`ov`: text, x, y, size, color, stroke, box, anchor, max_width, family).

    Se dibuja en una capa del tamaño del bloque (no del fotograma) para que sea barato por fotograma.
    """
    alpha = ease(progress / 0.25 if ov.get("anim", "fade") != "none" else 1.0, "out")
    if alpha <= 0:
        return
    W, H = img.size
    size = ov.get("size", 64) * scale
    pop = 1.0
    if ov.get("anim") == "pop":
        pop = 0.85 + 0.15 * ease(progress / 0.2, "out")
    fnt = font(ov.get("family", "Inter"), int(size * pop), ov.get("style", "Bold"))
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = wrap(measure, ov["text"], fnt, ov.get("max_width", 0.8) * W)
    line_h = fnt.size * 1.18
    block_w = max(measure.textlength(line, font=fnt) for line in lines)
    block_h = line_h * len(lines)
    x, y = ov.get("x", 0.5) * W, ov.get("y", 0.85) * H
    anchor = ov.get("anchor", "center")
    x0 = x - block_w / 2 if anchor == "center" else (x - block_w if anchor == "right" else x)
    y0 = y - block_h / 2
    # Zona segura: el bloque nunca se sale del encuadre (p. ej. overlays 16:9 en un recorte 9:16).
    margin = ov.get("safe_margin", 0.05) * W
    x0 = min(max(x0, margin), max(margin, W - margin - block_w))
    y0 = min(max(y0, margin), max(margin, H - margin - block_h))
    stroke = int(ov.get("stroke", 0) * scale)
    pad = 0.35 * fnt.size if ov.get("box") else 0.0
    m = int(pad + stroke + 4)
    layer = Image.new("RGBA", (int(block_w) + 2 * m, int(block_h) + 2 * m), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if ov.get("box"):
        d.rounded_rectangle([m - pad, m - pad * 0.6, m + block_w + pad, m + block_h + pad * 0.6],
                            radius=pad, fill=rgba(ov["box"], alpha * ov.get("box_alpha", 0.92)))
    for i, line in enumerate(lines):
        lw = d.textlength(line, font=fnt)
        lx = (block_w - lw) / 2 if anchor == "center" else (block_w - lw if anchor == "right" else 0)
        d.text((m + lx, m + i * line_h), line, font=fnt, fill=rgba(ov.get("color", "#FFFFFF"), alpha),
               stroke_width=stroke, stroke_fill=rgba(ov.get("stroke_color", "#000000"), alpha))
    _composite(img, layer, int(x0) - m, int(y0) - m)


def _composite(img: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    """alpha_composite recortando a los límites del fotograma."""
    l, t = max(0, -x), max(0, -y)
    r, b = min(layer.width, img.width - x), min(layer.height, img.height - y)
    if r > l and b > t:
        img.alpha_composite(layer.crop((l, t, r, b)), (x + l, y + t))


def _chart_layer(img: Image.Image, x: float, y: float, w: float, h: float, scale: float):  # noqa: ANN202
    """Capa del tamaño del gráfico (con margen para etiquetas) y su origen en el fotograma."""
    m = int(90 * scale)
    ox, oy = int(x) - m, int(y) - m
    layer = Image.new("RGBA", (int(w) + 2 * m, int(h) + 2 * m), (0, 0, 0, 0))
    return layer, ox, oy


def draw_counter(img: Image.Image, ov: dict, progress: float, scale: float, elapsed: float) -> None:
    """Contador animado entre `from` y `to` en `duration` segundos, con formato exacto."""
    dur = max(ov.get("duration", 1.2), 0.01)
    v = ov["from"] + (ov["to"] - ov["from"]) * ease(elapsed / dur, "out")
    text = ov.get("prefix", "") + format(v, ov.get("format", ",.0f")) + ov.get("suffix", "")
    draw_text(img, {**ov, "text": text}, progress, scale)


def draw_bar_chart(img: Image.Image, ov: dict, progress: float, scale: float, elapsed: float) -> None:
    """Barras desde datos: ov.data = [[label, value], ...]; crecen durante `duration`."""
    W, H = img.size
    x, y, w, h = ov.get("x", 0.15) * W, ov.get("y", 0.2) * H, ov.get("w", 0.7) * W, ov.get("h", 0.6) * H
    data = ov["data"]
    vmax = ov.get("max") or max(v for _, v in data) or 1
    grow = ease(elapsed / ov.get("duration", 1.0), "out")
    alpha = ease(progress / 0.15, "out")
    layer, ox, oy = _chart_layer(img, x, y, w, h, scale)
    x, y = x - ox, y - oy
    d = ImageDraw.Draw(layer)
    n = len(data)
    gap = w * 0.08 / max(n, 1)
    bw = (w - gap * (n + 1)) / n
    lab_f = font(ov.get("family", "Inter"), int(ov.get("label_size", 34) * scale), "SemiBold")
    val_f = font(ov.get("family", "Inter"), int(ov.get("value_size", 40) * scale), "Bold")
    colors = ov.get("colors", ["#3B82F6"])
    base_y = y + h
    d.line([x, base_y, x + w, base_y], fill=rgba(ov.get("axis_color", "#FFFFFF"), alpha), width=max(int(3 * scale), 1))
    for i, (label, value) in enumerate(data):
        bh = (value / vmax) * (h - val_f.size * 1.6) * grow
        bx = x + gap + i * (bw + gap)
        d.rounded_rectangle([bx, base_y - bh, bx + bw, base_y], radius=int(10 * scale),
                            fill=rgba(colors[i % len(colors)], alpha))
        txt = ov.get("prefix", "") + format(value * grow, ov.get("format", ",.0f")) + ov.get("suffix", "")
        tw = d.textlength(txt, font=val_f)
        d.text((bx + (bw - tw) / 2, base_y - bh - val_f.size * 1.3), txt, font=val_f,
               fill=rgba(ov.get("value_color", "#FFFFFF"), alpha))
        lw = d.textlength(str(label), font=lab_f)
        d.text((bx + (bw - lw) / 2, base_y + lab_f.size * 0.4), str(label), font=lab_f,
               fill=rgba(ov.get("label_color", "#FFFFFF"), alpha))
    _composite(img, layer, ox, oy)


def draw_line_chart(img: Image.Image, ov: dict, progress: float, scale: float, elapsed: float) -> None:
    """Línea desde datos (ov.points = [[x, y], ...]) que se dibuja progresivamente."""
    W, H = img.size
    x, y, w, h = ov.get("x", 0.12) * W, ov.get("y", 0.18) * H, ov.get("w", 0.76) * W, ov.get("h", 0.62) * H
    pts = ov["points"]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = ov.get("ymin", min(ys)), ov.get("ymax", max(ys))
    draw_upto = ease(elapsed / ov.get("duration", 1.5), "in_out")
    alpha = ease(progress / 0.15, "out")
    layer, ox, oy = _chart_layer(img, x, y, w, h, scale)
    x, y = x - ox, y - oy
    d = ImageDraw.Draw(layer)
    axis = rgba(ov.get("axis_color", "#FFFFFF"), alpha)
    d.line([x, y + h, x + w, y + h], fill=axis, width=max(int(3 * scale), 1))
    d.line([x, y, x, y + h], fill=axis, width=max(int(3 * scale), 1))

    def to_px(px: float, py: float) -> tuple[float, float]:
        return (x + (px - xmin) / ((xmax - xmin) or 1) * w, y + h - (py - ymin) / ((ymax - ymin) or 1) * h)

    n_vis = max(2, int(round(draw_upto * (len(pts) - 1))) + 1)
    seg = [to_px(*p) for p in pts[:n_vis]]
    if len(seg) >= 2:
        d.line(seg, fill=rgba(ov.get("color", "#F59E0B"), alpha), width=max(int(ov.get("width", 8) * scale), 2),
               joint="curve")
    lab_f = font(ov.get("family", "Inter"), int(ov.get("label_size", 30) * scale), "SemiBold")
    for lbl in ov.get("x_labels", []):  # [[x, "texto"], ...]
        px, _ = to_px(lbl[0], ymin)
        tw = d.textlength(str(lbl[1]), font=lab_f)
        d.text((px - tw / 2, y + h + lab_f.size * 0.4), str(lbl[1]), font=lab_f, fill=axis)
    _composite(img, layer, ox, oy)


OVERLAYS = {"text": draw_text, "counter": draw_counter, "bar_chart": draw_bar_chart, "line_chart": draw_line_chart}


def card(width: int, height: int, title: str, subtitle: str | None = None, bg: str = "#111827",
         fg: str = "#FFFFFF", family: str = "Inter") -> Image.Image:
    """Tarjeta simple (títulos, capítulos) como imagen de fondo de escena."""
    img = Image.new("RGBA", (width, height), rgba(bg))
    s = height / 1080
    draw_text(img, {"text": title, "x": 0.5, "y": 0.45, "size": 96, "color": fg, "family": family, "anim": "none"}, 1, s)
    if subtitle:
        draw_text(img, {"text": subtitle, "x": 0.5, "y": 0.6, "size": 48, "color": fg, "family": family,
                        "style": "Regular", "anim": "none"}, 1, s)
    return img
