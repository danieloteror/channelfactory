# Operación, recuperación y despliegue

## Flujo mensual
```bash
channel-factory plan-month 2026-11 --channel <id>        # lote único por canal+mes (repetirlo no duplica)
# El agente en sesión escribe episode.yaml + claims.json de cada episodio (o un LLM con presupuesto)
channel-factory produce-month 2026-11 --channel <id>     # encola etapas con dependencias e idempotencia
channel-factory worker                                    # procesa la cola (proceso persistente)
channel-factory status --channel <id>                     # progreso, bloqueos y errores accionables
```
Etapas por episodio: `episode.script → narration → render → assemble → qa → clips → publish.prepare`.
Con la publicación desactivada (por defecto) los episodios terminan en `ready_to_schedule` con todo en
`render/`: `master_clean.mp4`, `subtitles_en.srt/.vtt`, `transcript.json`, `scenes.json`, `thumbnail.jpg`,
`qa_technical.json`.

## Recuperación
- **Worker caído o reiniciado:** los leases caducan y el siguiente `claim` recupera el trabajo; las
  etapas terminadas no se repiten (clave idempotente `episodio:etapa:v<guion>` + caché de artefactos).
- **Bloqueos (`blocked`):** falta de acceso, modelo, voz, guion o claims. `status` dice qué falta;
  tras resolverlo: `channel-factory resume <batch_id>` o `channel-factory retry <job_id>`.
- **Fallos transitorios:** reintentos con backoff (`runtime.retry_backoff_seconds`), máximo `max_attempts`.
- **Cambio de guion:** sube `script_version`; solo se regeneran las escenas cuyo texto/visual cambió.
- **Retención:** los medios necesarios para publicaciones futuras se protegen (`retain_until`).

## Despliegue 24/7
- **VPS / servidor Linux:** `channel-factory worker` como servicio systemd (`Restart=always`,
  `KillSignal=SIGTERM`: termina el trabajo actual y sale). Un solo worker de trabajos pesados
  (`heavy_jobs_concurrency: 1`) hasta medir margen de CPU/RAM.
- **Mac:** `launchd` con `KeepAlive`. Si el Mac duerme o se apaga, la cola se detiene y continúa al
  despertar (los leases caducados se recuperan). Usa `caffeinate -i channel-factory worker` durante lotes largos.
- La sesión de Claude no sustituye al worker: dirige, escribe guiones y revisa.

## Rendimiento medido (entorno de desarrollo, 4 CPU, sin GPU)
- Kokoro (CPU): RTF ≈ 0,5–1,0 → 8 min de narración ≈ 4–8 min de síntesis.
- Compositor a 1080p30: ~40 ms/fotograma por proceso con OpenCV instalado (~100 ms solo con Pillow).
  8 min = 14 400 fotogramas ≈ 10 min con 1 proceso; `runtime.render_workers` reparte escenas entre núcleos.
- Los tiempos reales se registran por trabajo; ajusta la concurrencia con datos, no a ojo.

## Costes
`budget.paid_providers_enabled: false` por defecto: solo proveedores locales/gratuitos. Para activar
proveedores de pago define topes por episodio, lote y mes; sin tope no se gasta (`BudgetBlocked`).
`channel-factory budget` muestra reservas, cargos y liberaciones por categoría (llm, image, video, tts…).
