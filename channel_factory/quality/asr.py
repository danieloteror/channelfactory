"""ASR local (Whisper base.en vía sherpa-onnx) para QA de narración y transcripciones.

Modelo: k2-fsa/sherpa-onnx releases `asr-models/sherpa-onnx-whisper-base.en`
(conversión ONNX de OpenAI Whisper, MIT). Funciona sin red tras la descarga.
Precisión: texto por segmento; no inventa tiempos de palabra.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

ASR_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-base.en.tar.bz2"
MODEL_SUBDIR = "asr/sherpa-onnx-whisper-base.en"


def load_audio_16k(path: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


class WhisperASR:
    def __init__(self, models_dir: Path, int8: bool = True):
        d = Path(models_dir) / MODEL_SUBDIR
        suffix = ".int8.onnx" if int8 else ".onnx"
        self.encoder = d / f"base.en-encoder{suffix}"
        self.decoder = d / f"base.en-decoder{suffix}"
        self.tokens = d / "base.en-tokens.txt"
        self._rec = None

    def available(self) -> bool:
        return all(p.exists() for p in (self.encoder, self.decoder, self.tokens))

    def _recognizer(self):  # noqa: ANN202
        if self._rec is None:
            import sherpa_onnx

            self._rec = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(self.encoder), decoder=str(self.decoder), tokens=str(self.tokens),
                language="en", task="transcribe", num_threads=2,
            )
        return self._rec

    def transcribe_array(self, audio: np.ndarray, sr: int = 16000, window_s: float = 28.0) -> str:
        rec = self._recognizer()
        out = []
        step = int(window_s * sr)
        for i in range(0, max(len(audio), 1), step):
            chunk = audio[i:i + step]
            if len(chunk) < sr * 0.3:
                continue
            s = rec.create_stream()
            s.accept_waveform(sr, chunk)
            rec.decode_stream(s)
            out.append(s.result.text.strip())
        return " ".join(t for t in out if t)

    def transcribe(self, path: Path) -> str:
        return self.transcribe_array(load_audio_16k(path))


def _words(text: str) -> list[str]:
    """Forma canónica: sin pausas, cifras en palabras, sin guiones ni mayúsculas."""
    from ..providers.tts_kokoro import normalize

    text = re.sub(r"\[pause [\d.]+\]", " ", text)
    text = normalize(text).lower().replace("-", " ")
    return re.findall(r"[a-z']+", text)


@dataclass
class RoundTrip:
    wer: float
    missing: list[str]
    inserted: list[str]
    ok: bool


def roundtrip(expected: str, heard: str, max_wer: float = 0.12) -> RoundTrip:
    """Compara el guion (antes del léxico) con lo que transcribe el ASR, en forma canónica."""
    a, b = _words(expected), _words(heard)
    sm = SequenceMatcher(a=a, b=b, autojunk=False)
    missing, inserted = [], []
    edits = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        edits += max(i2 - i1, j2 - j1)
        if tag in ("delete", "replace"):
            missing.append(" ".join(a[i1:i2]))
        if tag in ("insert", "replace"):
            inserted.append(" ".join(b[j1:j2]))
    wer = edits / max(len(a), 1)
    return RoundTrip(round(wer, 3), missing, inserted, wer <= max_wer)
