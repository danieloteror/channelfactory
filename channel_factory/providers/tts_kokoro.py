"""Kokoro-82M vía kokoro-onnx (CPU) con normalización y léxico de pronunciación.

Modelos en un directorio persistente (no se descargan en cada trabajo). Fuente:
GitHub releases de thewh1teagle/kokoro-onnx (model-files-v1.0), conversión ONNX
de hexgrad/Kokoro-82M (Apache-2.0). Funciona sin red una vez descargado.
En Apple Silicon puede sustituirse por MLX-Audio con la misma interfaz.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np

from .base import Health, SynthResult, VoiceInfo

MODEL_FILES = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "kokoro-v1.0.int8.onnx": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/{name}"

_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_to_words(n: int) -> str:
    if n < 0:
        return "minus " + number_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + number_to_words(rest))
    # 1100–9999 que no son múltiplos de mil se leen "twelve hundred" en inglés hablado
    if n < 10000 and n % 100 == 0 and n % 1000 != 0:
        return number_to_words(n // 100) + " hundred"
    for unit, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= unit:
            rest = n % unit
            return number_to_words(n // unit) + " " + name + ("" if rest == 0 else " " + number_to_words(rest))
    return str(n)


def _decimal(m: re.Match) -> str:
    whole, frac = m.group(1).replace(",", ""), m.group(2)
    return number_to_words(int(whole)) + " point " + " ".join(_ONES[int(d)] for d in frac)


def normalize(text: str, lexicon: dict[str, str] | None = None) -> str:
    """Normaliza cifras y aplica respellings del léxico (antes de fonemizar)."""
    t = text
    for word, say in (lexicon or {}).items():
        t = re.sub(rf"\b{re.escape(word)}\b", say, t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s?%", lambda m: m.group(1) + " percent", t)
    # Años tras preposición: "in 1990" -> "in nineteen ninety", "since 2008" -> "since two thousand eight"
    t = re.sub(
        r"\b(in|since|by|from|until|of|year)\s+(19\d{2}|20[1-9]\d)\b",
        lambda m: m.group(1) + " " + number_to_words(int(m.group(2)[:2])) + " " + (
            number_to_words(int(m.group(2)[2:])) if int(m.group(2)[2:]) >= 10 else "oh " + number_to_words(int(m.group(2)[2:]))
        ),
        t,
    )
    t = re.sub(r"\b(\d{1,3}(?:,\d{3})*|\d+)\.(\d+)\b", _decimal, t)
    t = re.sub(r"\b(\d{1,3}(?:,\d{3})+|\d+)\b", lambda m: number_to_words(int(m.group(1).replace(",", ""))), t)
    t = t.replace("&", " and ").replace("—", ", ").replace("–", " to ")
    return re.sub(r"\s+", " ", t).strip()


_PAUSE = re.compile(r"\[pause\s+(\d+(?:\.\d+)?)\]")


class KokoroTTS:
    provider_id = "kokoro_onnx"

    def __init__(self, models_dir: Path, quantized: bool = False, lexicon: dict[str, str] | None = None):
        self.models_dir = Path(models_dir) / "kokoro"
        self.model_name = "kokoro-v1.0.int8.onnx" if quantized else "kokoro-v1.0.onnx"
        self.lexicon = lexicon or {}
        self._k = None

    # ------------------------------------------------------------ estado
    def missing_files(self) -> list[str]:
        return [n for n in (self.model_name, "voices-v1.0.bin") if not (self.models_dir / n).exists()]

    def health(self) -> Health:
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return Health("blocked", "paquete kokoro-onnx no instalado (pip install '.[tts]')", {"package": False})
        missing = self.missing_files()
        if missing:
            return Health("blocked", f"faltan modelos {missing}; ejecuta `channel-factory voices preload`",
                          {"package": True, "models": False})
        return Health("ok", f"{self.model_name} en {self.models_dir}", {"package": True, "models": True})

    def verify_checksums(self) -> dict[str, bool]:
        out = {}
        for name in (self.model_name, "voices-v1.0.bin"):
            p = self.models_dir / name
            if p.exists():
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                out[name] = h == MODEL_FILES[name]
        return out

    def _engine(self):  # noqa: ANN202
        if self._k is None:
            from kokoro_onnx import Kokoro

            if self.missing_files():
                raise FileNotFoundError(f"modelos Kokoro ausentes en {self.models_dir}")
            self._k = Kokoro(str(self.models_dir / self.model_name), str(self.models_dir / "voices-v1.0.bin"))
        return self._k

    # ------------------------------------------------------------ API
    def voices(self) -> list[VoiceInfo]:
        out = []
        for v in sorted(self._engine().get_voices()):
            prefix = v[:2]
            if prefix not in ("af", "am", "bf", "bm"):
                continue
            out.append(VoiceInfo(v, "en-US" if prefix[0] == "a" else "en-GB",
                                 "female" if prefix[1] == "f" else "male",
                                 "American" if prefix[0] == "a" else "British"))
        return out

    def estimate_cost(self, text: str) -> float:
        return 0.0  # local

    def synthesize_array(self, text: str, voice_id: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        """Sintetiza respetando marcas [pause N] como silencios explícitos."""
        k = self._engine()
        parts = _PAUSE.split(text)
        chunks: list[np.ndarray] = []
        sr = 24000
        for i, part in enumerate(parts):
            if i % 2 == 1:
                chunks.append(np.zeros(int(float(part) * sr), dtype=np.float32))
                continue
            part = normalize(part, self.lexicon)
            if not part:
                continue
            samples, sr = k.create(part, voice=voice_id, speed=speed, lang="en-us" if voice_id[0] == "a" else "en-gb")
            chunks.append(samples.astype(np.float32))
        audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        return audio, sr

    def synthesize(self, text: str, voice_id: str, out_path: Path, speed: float = 1.0) -> SynthResult:
        import soundfile as sf

        audio, sr = self.synthesize_array(text, voice_id, speed)
        audio = limit_peak(trim_silence(audio, sr))
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, audio, sr, subtype="PCM_24")
        return SynthResult(out_path, len(audio) / sr, sr, voice_id, "segment")


def limit_peak(audio: np.ndarray, ceiling_dbfs: float = -1.0) -> np.ndarray:
    """Kokoro puede superar 0 dBFS; escala para evitar clipping al escribir PCM."""
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    ceiling = 10 ** (ceiling_dbfs / 20)
    return audio * (ceiling / peak) if peak > ceiling else audio


def trim_silence(audio: np.ndarray, sr: int, thresh_db: float = -50.0, pad_s: float = 0.06) -> np.ndarray:
    """Recorta silencio inicial/final (con margen) para medir duraciones reales."""
    if audio.size == 0:
        return audio
    thr = 10 ** (thresh_db / 20)
    idx = np.where(np.abs(audio) > thr)[0]
    if idx.size == 0:
        return audio[:0]
    pad = int(pad_s * sr)
    return audio[max(0, idx[0] - pad): min(audio.size, idx[-1] + pad)]
