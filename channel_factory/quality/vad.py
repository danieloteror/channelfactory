"""Silero VAD vía onnxruntime, sin torch.

Usa el modelo ONNX que trae el wheel `silero-vad` (MIT): `pip install silero-vad --no-deps`
basta (localizamos el archivo sin importar el paquete, que exigiría torch). Replica el
contrato de su OnnxWrapper: bloques de 512 muestras a 16 kHz con 64 de contexto y estado (2,1,128).
Da intervalos donde probablemente hay habla; no sustituye la alineación de palabras.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

SR, CHUNK, CONTEXT = 16000, 512, 64


def model_path() -> Path | None:
    spec = importlib.util.find_spec("silero_vad")
    if not spec or not spec.submodule_search_locations:
        return None
    p = Path(list(spec.submodule_search_locations)[0]) / "data" / "silero_vad.onnx"
    return p if p.exists() else None


class SileroVAD:
    def __init__(self, path: Path | None = None):
        import onnxruntime

        path = path or model_path()
        if path is None:
            raise FileNotFoundError("modelo Silero no encontrado (pip install silero-vad --no-deps)")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])

    def probs(self, audio: np.ndarray) -> np.ndarray:
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, CONTEXT), dtype=np.float32)
        n = int(np.ceil(len(audio) / CHUNK))
        audio = np.pad(audio.astype(np.float32), (0, n * CHUNK - len(audio)))
        out = np.empty(n, dtype=np.float32)
        sr = np.array(SR, dtype=np.int64)
        for i in range(n):
            x = np.concatenate([context, audio[i * CHUNK:(i + 1) * CHUNK][None, :]], axis=1)
            prob, state = self.session.run(None, {"input": x, "state": state, "sr": sr})
            out[i] = float(prob.reshape(-1)[0])
            context = x[:, -CONTEXT:]
        return out

    def segments(self, audio: np.ndarray, threshold: float = 0.5, min_speech_s: float = 0.25,
                 min_silence_s: float = 0.1) -> list[tuple[float, float]]:
        p = self.probs(audio)
        step = CHUNK / SR
        neg = max(threshold - 0.15, 0.01)
        segs, start, silence_since = [], None, None
        for i, v in enumerate(p):
            t = i * step
            if start is None:
                if v >= threshold:
                    start, silence_since = t, None
            elif v < neg:
                silence_since = silence_since if silence_since is not None else t
                if t - silence_since >= min_silence_s:
                    if silence_since - start >= min_speech_s:
                        segs.append((round(start, 3), round(silence_since, 3)))
                    start, silence_since = None, None
            else:
                silence_since = None
        if start is not None and len(p) * step - start >= min_speech_s:
            segs.append((round(start, 3), round(len(p) * step, 3)))
        return segs
