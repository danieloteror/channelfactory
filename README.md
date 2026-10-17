# Channel Factory

**ES** · Fábrica de videos explicativos en inglés: investiga canales de referencia, produce episodios
horizontales de 6–10 minutos (narración, escenas con capas y cámara, gráficos exactos, música con
ducking, subtítulos, miniatura y QA), encarga los clips verticales a **Clip Master** y deja todo listo
para publicar. Estado durable en SQLite: los trabajos sobreviven a reinicios y no dependen de una
sesión de chat abierta.

**EN** · A factory for English explainer videos: it researches reference channels, produces 6–10 minute
horizontal episodes (narration, layered scenes with camera moves, data-exact graphics, ducked music,
subtitles, thumbnail and QA), hands vertical clipping to **Clip Master**, and leaves everything ready to
publish. Durable SQLite state: jobs survive restarts and don't need an open chat session.

> Este repositorio contiene **solo la herramienta**. Los canales (perfil, biblia, personajes, voces,
> episodios) se crean en local con `channel-factory channel init` y se registran en
> `config/factory.local.yaml` (fuera de Git).
>
> This repository holds **only the tool**. Channels are created locally with `channel-factory channel init`.

> **Agente de Claude Code / Claude Code agent:** abre este repo con Claude Code y pega el enlace de un
> canal de YouTube (o usa `/analizar-canal <url>`): analiza qué le funcionó más, por qué (hipótesis con
> evidencia y confianza) y cómo está producido, y entrega `format_bible.md` y propuestas originales. La
> especificación completa está en [`MASTER_PROMPT.md`](MASTER_PROMPT.md) y las instrucciones del agente en
> [`CLAUDE.md`](CLAUDE.md). / Open the repo in Claude Code and paste a YouTube channel link (or run
> `/analizar-canal <url>`).

> **Publicación / Publishing:** la integración con Metricool está implementada pero **desactivada**
> (`publishing.enabled: false`). Se recomienda para automatizar; cómo activarla en
> [docs/metricool.md](docs/metricool.md) (ES/EN). / Metricool integration is implemented but **disabled**;
> recommended for automation — see [docs/metricool.md](docs/metricool.md).

---

## Español

### El agente
| Pieza | Qué es |
|---|---|
| [`MASTER_PROMPT.md`](MASTER_PROMPT.md) | Especificación completa del producto (tal cual, fuente de verdad) |
| [`CLAUDE.md`](CLAUDE.md) | Cómo opera el agente: flujos, reglas y mapa del código |
| `.claude/skills/analizar-canal` | `/analizar-canal <url> [canal]`: qué le funcionó, por qué, producción, format bible y propuestas |
| `.claude/skills/producir-episodio` | `/producir-episodio <canal> <tema>`: guion → visual → render → revisión |
| `.claude/skills/producir-mes` | `/producir-mes <canal> <YYYY-MM>`: lote mensual con cola durable |
| `.claude/agents/` | Subagentes con contrato: `investigador-canales`, `guionista`, `director-visual`, `revisor`, `editor-clips` |
| [`docs/styles.md`](docs/styles.md) | 5 skills de estilo visual (HyperFrames, onetake, Collage B-roll, Claude Animation, Lemo-Opuscar) |

El análisis funciona con `YOUTUBE_API_KEY` (catálogo completo con fechas) o, sin ella, con yt-dlp
(pestañas /videos, /shorts y /streams del canal + metadatos de los videos más vistos y recientes).

### Instalación
Requisitos: Python 3.11+, FFmpeg/ffprobe, fontconfig. Funciona en Mac Apple Silicon o Linux sin GPU.
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[tts,asr,analysis,dev]"
pip install silero-vad --no-deps          # VAD por ONNX, sin torch (opcional)
cp .env.example .env                      # rellena solo lo que uses
channel-factory setup                     # carpetas de estado + modelo de voz Kokoro (con checksum)
channel-factory models fetch-asr          # Whisper base.en local para verificar la narración
channel-factory doctor --smoke            # diagnóstico + episodio sintético de prueba
```

### Uso
```bash
channel-factory channel init <id> --name "<Nombre>" --niche <nicho>   # perfil independiente por canal
channel-factory analyze-channel <url> [--channel <id>] [--download-media]
channel-factory analyze-video <url-o-archivo> [--channel <id>]
channel-factory voices audition --channel <id>      # 5–8 muestras + verificación ASR + galería HTML
channel-factory voices select <voz> --channel <id>  # fija la voz (o --provisional para pilotos)
channel-factory episode new <ep> --channel <id> --title "..."
channel-factory episode check <ep> --channel <id>   # assets, claims y anclas de tiempo
channel-factory episode render <ep> --channel <id> [--vertical-preview]
channel-factory episode clips <ep> --channel <id>   # Clip Master (cuando esté configurado)
channel-factory plan-month 2026-11 --channel <id>   # lote idempotente con slots y backlog
channel-factory produce-month 2026-11 --channel <id>
channel-factory worker                               # proceso persistente que ejecuta la cola
channel-factory status --channel <id> | resume <batch_id> | retry <job_id> [--stage …] | budget
```
El guion de cada episodio es `channels/<id>/episodes/<ep>/episode.yaml` (escenas con texto, beat,
capas, cámara, overlays y ROI vertical) + `claims.json` (afirmación, fuente, fecha, certeza). Lo
escribe el agente en sesión o un LLM con presupuesto; las duraciones salen del audio real.

### Módulos
| Módulo | Responsabilidad |
|---|---|
| `reference_research/` | YouTube Data API, yt-dlp, cohortes, análisis audiovisual (cortes, keyframes, movimiento, paleta, voz, ritmo) |
| `editorial/` | `claims.json` verificable, planificación mensual idempotente |
| `series/` | Biblia persistente y referencias aprobadas de personajes |
| `production/` | Voces, narración por escena, compositor, gráficos, mezcla, subtítulos, miniatura |
| `quality/` | QA técnica (ffprobe/ebur128/negros/silencios), ASR ida y vuelta, VAD |
| `clip_master_adapter/` | Contrato v1.0, perfil `original_explainer`, pistas narrativas, validación |
| `publishing/` | Metricool (desactivado): payload, validación, ledger, transportes, almacenamiento |
| `orchestrator/` | Cola con leases, worker, presupuesto con reservas, caché de artefactos |
| `providers/` | Interfaces de TTS/imagen/LLM, Kokoro, registro de herramientas propias |

### Qué funciona hoy y qué está bloqueado
Funciona y está probado (41 tests + prueba de punta a punta): cola durable con recuperación,
idempotencia y presupuesto; audiciones y narración con Kokoro; verificación ASR; compositor
16:9 y 9:16 con recomposición; mezcla a −14 LUFS / −1,5 dBTP; subtítulos SRT/VTT; QA técnica;
análisis audiovisual local; contrato y validación de Clip Master con un doble de prueba.

Pendiente de datos o accesos (lo muestra `channel-factory doctor`):
- **Clip Master:** ruta/repositorio y forma de invocarlo (`clip_master.path` + `command`).
- **Análisis de canales:** `YOUTUBE_API_KEY`; yt-dlp necesita acceso a youtube.com.
- **Imágenes generativas:** no hay proveedor configurado; registra el tuyo en `providers/user_tools.yaml`
  (o API con presupuesto). El compositor usa las imágenes/capas que aporte el canal o un MP4 de una skill
  de estilo como fondo de escena ([docs/styles.md](docs/styles.md)); esas skills no se instalan automáticamente.
- **Metricool:** desactivado a propósito (ver arriba).

Más: [operación y despliegue](docs/operations.md) · [contrato Clip Master](docs/clip_master_contract.md) ·
[herramientas inspeccionadas](docs/tools/inspection.md) · [modelos](docs/models.md).

---

## English

### Install
Python 3.11+, FFmpeg/ffprobe and fontconfig. Runs on Apple Silicon Macs or Linux without a GPU.
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[tts,asr,analysis,dev]"
pip install silero-vad --no-deps
cp .env.example .env
channel-factory setup && channel-factory models fetch-asr && channel-factory doctor --smoke
```

### The agent
Open the repo in Claude Code and paste a YouTube channel link, or run `/analizar-canal <url>`. It reports
what worked best for the channel (formats, topics, titles, duration, thumbnails, hooks — with numbers),
why (hypotheses with evidence and confidence), how it is produced, a `format_bible.md` and original
proposals. Full spec: [`MASTER_PROMPT.md`](MASTER_PROMPT.md); agent rules: [`CLAUDE.md`](CLAUDE.md);
visual style skills: [`docs/styles.md`](docs/styles.md).

### Use
Create a channel profile (`channel init`), analyze references (`analyze-channel`, `analyze-video`),
audition and pick a voice (`voices audition`, `voices select`), write each episode's `episode.yaml` +
`claims.json`, then `episode check` and `episode render`. For a month: `plan-month` →
`produce-month` → `worker`. Inspect with `status`, recover with `resume`/`retry`.

### What works / what's blocked
Working and tested: durable queue (leases, recovery, idempotency, budget reservations), Kokoro
auditions and narration, ASR round-trip checks, 16:9 and 9:16 compositor with recomposition,
loudness-normalized mix, SRT/VTT subtitles, technical QA, local audiovisual analysis, Clip Master
contract and validation (tested against a test double).
Needs your input: Clip Master path/invocation, `YOUTUBE_API_KEY`, an image provider of your own.
Metricool publishing is implemented but intentionally disabled — see [docs/metricool.md](docs/metricool.md).
