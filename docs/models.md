# Modelos y dependencias

Directorio persistente: `~/.cache/channel_factory/models` (o `CHANNEL_FACTORY_MODELS_DIR`). Nada se
descarga por trabajo; tras la descarga todo funciona sin red.

| Modelo | Fuente | Licencia | Tamaño | SHA-256 |
|---|---|---|---|---|
| Kokoro-82M ONNX fp32 `kokoro-v1.0.onnx` | github.com/thewh1teagle/kokoro-onnx releases `model-files-v1.0` | Apache-2.0 (pesos hexgrad/Kokoro-82M) | 325,5 MB | `7d5df8ec…3636a6c5` |
| Kokoro-82M ONNX int8 `kokoro-v1.0.int8.onnx` (opcional, `--quantized`) | ídem | Apache-2.0 | 92,4 MB | `6e742170…f6406cb` |
| Voces `voices-v1.0.bin` (54 voces) | ídem | Apache-2.0 | 28,2 MB | `bca610b8…8fbf7d` |
| Whisper base.en (sherpa-onnx) | github.com/k2-fsa/sherpa-onnx releases `asr-models` | MIT (OpenAI Whisper) | ~208 MB comprimido | — |
| Silero VAD ONNX | dentro del wheel `silero-vad` 6.2.3 | MIT | <3 MB | — |

Checksums completos en `channel_factory/providers/tts_kokoro.py` (`MODEL_FILES`); `voices preload`
los verifica y escribe `MANIFEST.json`.

No se descargan automáticamente modelos de video (LTX-Video, Wan2.2) ni de separación de stems:
requieren medir memoria/tiempo y revisar licencias antes de activarse.

Apple Silicon: Kokoro ONNX funciona en CPU; un adaptador MLX-Audio puede sustituirlo con la misma
interfaz `TTSProvider` (pendiente).
