# Herramientas inspeccionadas (2026-10-17)

Registro máquina: [`providers/registry.yaml`](../../providers/registry.yaml). Inspección por lectura de
README/LICENSE/código en clones aislados; no se ha copiado código de terceros al repositorio.

## Faceless YouTube Agents (base declarada)
- Commit `e92c845` (2026-04-30). Licencia: **MIT declarada en el README, sin archivo LICENSE** → no se
  redistribuye su código; se reutiliza el diseño.
- Flujo: análisis de canal con yt-dlp (top 20 + subtítulos) → guion → `slides.json` → imágenes (Gathos)
  → TTS (Gathos) → montaje FFmpeg (`zoompan` sobre imagen escalada a 8000 px + `xfade` 0,5 s, 24 fps)
  → miniatura (Gathos) → subida privada opcional a YouTube (OAuth, subida reanudable).
- Limitaciones: Gathos obligatorio para voz e imagen; duración estimada por palabras (2,5 pal/s);
  diapositivas de 3–10 s; estado en un JSON por ejecución (no es una cola durable; el «autopilot» lo
  conduce el agente en sesión); `media_host` usa file.io (enlaces que caducan: no válido para Metricool).
- Integración en Channel Factory: mismas etapas, pero con proveedores intercambiables (Kokoro local por
  defecto; Gathos registrable como herramienta propia), duración tomada del audio real, compositor con
  cámara subpíxel y capas, cola SQLite con leases, caché por contenido y QA técnica.

## Agent-Reach (MIT, `94f06c1`)
CLI para que el agente lea YouTube (subtítulos y búsqueda vía yt-dlp), web y redes, con `agent-reach
doctor`. Útil en el Mac para investigación en sesión. Instalar desde el repositorio (el paquete homónimo
de PyPI no es este proyecto). Cookies solo locales.

## Graphify (Apache-2.0/MIT, `86c57be`)
`uv tool install graphifyy` → `/graphify .` construye `graph.json` de código y docs;
`graphify query|path|explain` para consultas pequeñas. Recomendado sobre `channel_factory/` y `docs/`.

## HyperFrames (Apache-2.0, `09c0307`, CLI 0.8.x)
HTML/CSS/GSAP → MP4 determinista con Chrome headless; Node 22+ y FFmpeg. Pendiente de prueba: el
backend inicial elegido es el compositor Python+FFmpeg (probado). Si se adopta para títulos/capas
animadas, cargar solo los workflows necesarios (`npx hyperframes skills update`), no los 21 skills.

## Herk Edit Kit (MIT, `10511e5`)
Construido sobre HyperFrames. `short-form-edit` (hook/payoff, subtítulos precisos, B-roll, revisión de
audio, validadores) y `style-library` (tarjetas, tokens; incluye estilo tipo explainer). Sus criterios
inspiran el perfil `original_explainer`; también aplicables a los flujos de fútbol/gameplay de Clip Master.

## Análisis visual y de audio (probados en este entorno)
| Herramienta | Versión | Licencia | Uso |
|---|---|---|---|
| PySceneDetect | 0.7.2 | BSD-3 | Cortes (alternativa: filtro `scene` de FFmpeg) |
| OpenCV headless | 5.0.0.93 | Apache-2.0 | Flujo óptico: estático editado vs. animación continua |
| librosa | 1.0.0 | ISC | Tempo y onsets (audio decodificado con FFmpeg) |
| Silero VAD | 6.2.3 | MIT | Intervalos de habla vía ONNX sin torch (`--no-deps`) |
| python-audio-separator | 0.47.0 | MIT (modelos con licencia propia) | Opcional, solo para fragmentos difíciles; no instalado |
| sherpa-onnx + Whisper base.en | 1.13.8 | Apache-2.0 / MIT | QA de narración y transcripción local |

Estas señales preparan la revisión; no identifican por sí solas el estilo ni un momento extraordinario:
los keyframes se revisan con un modelo con visión o una persona.
