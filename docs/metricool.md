# Metricool — publicación automática (opcional) / Automated publishing (optional)

> **Estado actual / Current status:** implementado pero **desactivado** (`publishing.enabled: false`).
> Channel Factory no envía nada a Metricool. Los episodios terminan en `ready_to_schedule` con todo
> lo necesario para subirlos a mano en `channels/<canal>/episodes/<episodio>/render/`.
>
> Implemented but **disabled** (`publishing.enabled: false`). Channel Factory sends nothing to
> Metricool. Episodes end in `ready_to_schedule` with everything needed for a manual upload in
> `channels/<channel>/episodes/<episode>/render/`.

---

## Español

### ¿Por qué se recomienda?
Para automatizar el ciclo completo (producir → recortar → programar → confirmar) hace falta un
gestor de publicación con YouTube (largos y Shorts) y TikTok en una sola cola. Metricool lo cubre,
tiene MCP oficial y API, y Channel Factory ya trae la integración lista con las protecciones que
importan cuando se publica sin supervisión:

- **Deduplicación** por pieza/destino/versión: repetir un comando no duplica publicaciones.
- **Lock por publicación**: dos workers nunca envían la misma pieza.
- **Ambigüedad segura**: tras un timeout la pieza queda `unconfirmed` y se **concilia** con lo
  programado en Metricool antes de cualquier reintento (nunca se reenvía a ciegas).
- **Validación previa**: marca exacta del canal, red conectada, marcas protegidas de otros flujos,
  modo, video+audio, 9:16 para Shorts/TikTok, duración y peso, título, audiencia y URL pública
  persistente (se rechazan enlaces prefirmados que caducan antes de la fecha).
- **`scheduled` ≠ `published`**: solo se marca publicado con la URL final verificada.

### Qué hay implementado (`channel_factory/publishing/`)
| Archivo | Qué hace |
|---|---|
| `payload.py` | Construye el objeto `info` de `createScheduledPost` (YouTube video/short, TikTok) |
| `validate.py` | Validación previa; bloquea antes de enviar |
| `ledger.py` | Outbox/ledger: deduplicación, locks, estados, conciliación |
| `transports.py` | `mcp_handoff` (bandeja para la sesión de Claude) y `http_api` (API REST) |
| `storage.py` | Subida a almacenamiento con URL pública (S3/R2 público o Supabase) y verificación |
| `cli_ops.py` | `month schedule`: prepara y envía el lote según el modo |

### Cómo activarlo (cuando quieras)
1. En Metricool, conecta YouTube y TikTok a la **marca del canal** (no a la de otros flujos).
2. En `channels/<canal>/channel.yaml`: `metricool_brand_id`, `youtube_destination_id`, `tiktok_destination_id`.
3. Añade las marcas de otros flujos (p. ej. las de Clip Master) a `protected_brand_ids` en la config.
4. Configura almacenamiento público persistente: `publishing.media_storage: s3_public` (variables
   `CF_S3_BUCKET`, `CF_S3_PUBLIC_BASE_URL`, `CF_S3_ENDPOINT` y credenciales) o `supabase_public`.
5. Elige transporte:
   - `mcp_handoff` (todos los planes): el worker deja la llamada exacta en `var/outbox/metricool/`;
     la sesión de Claude la ejecuta con el MCP oficial y registra el resultado
     (`channel-factory publish record-result <pub_id> <respuesta.json>`). **No es desatendido.**
   - `http_api` (planes Advanced/Custom): `METRICOOL_USER_TOKEN` y `METRICOOL_USER_ID` en el entorno;
     acceso de red a `app.metricool.com`. Desatendido. Sin verificar todavía contra una cuenta real:
     haz primero una prueba en modo `draft`.
6. En `config/factory.yaml` (o `config/factory.local.yaml`): `publishing.enabled: true` y `mode: draft`.
7. Guarda las marcas actuales: pide a Claude `getBrandSettings` y ejecuta
   `channel-factory publish brands-refresh --from-json marcas.json`.
8. `channel-factory month schedule 2026-11 --channel <canal>` → revisa → `mode: autopilot` cuando estés seguro.

Conciliación: `getScheduledPosts` → `channel-factory publish reconcile --channel <canal> --remote-json programados.json`.
Confirmación: `channel-factory publish confirm <pub_id> <url-final>`.

### Límites documentados (revalidar antes de activar)
- Medios por URL: públicos y sin caducidad. TikTok vía Metricool: 3 s – 10 min, ≤ 500 MB.
- Shorts: límite conservador del proyecto 175 s (`defaults.clips.youtube_max_seconds`).
- Miniatura de video: según red y cuenta (TikTok solo Business). Sin campo de idioma documentado.
- Las claves de capacidades (`can_schedule_video`, `unattended`…) son internas, no campos de Metricool.

---

## English

### Why it's recommended
Full automation (produce → clip → schedule → confirm) needs one publishing queue for YouTube
(long-form and Shorts) and TikTok. Metricool covers that with an official MCP and an API, and
Channel Factory already ships the integration with the safeguards that matter when nobody is watching:
per piece/destination/version **deduplication**, a **per-publication lock**, **safe ambiguity**
(after a timeout the piece is `unconfirmed` and is **reconciled** against Metricool's scheduled
posts before any retry — never blind re-sends), **pre-flight validation** (exact brand, connected
network, protected brands from other flows, mode, video+audio, 9:16 for Shorts/TikTok, duration and
size, title, audience, persistent public URL — presigned links that expire before the date are
rejected), and **`scheduled` ≠ `published`** (published only with the verified final URL).

### What's implemented
See the table above (`channel_factory/publishing/`): payload builder, pre-flight validation,
ledger with dedupe/locks/reconciliation, transports (`mcp_handoff`, `http_api`), public media
storage (S3/R2 public bucket, Supabase) and the batch scheduler (`month schedule`).

### How to enable it (whenever you want)
1. In Metricool, connect YouTube and TikTok to the **channel's own brand**.
2. Set `metricool_brand_id`, `youtube_destination_id`, `tiktok_destination_id` in `channels/<channel>/channel.yaml`.
3. Add other flows' brands (e.g. Clip Master's) to `protected_brand_ids`.
4. Configure persistent public storage (`publishing.media_storage: s3_public` or `supabase_public`).
5. Pick a transport: `mcp_handoff` (any plan, **not unattended**: the Claude session executes the
   queued call with the official MCP and records the result) or `http_api` (Advanced/Custom plan,
   `METRICOOL_USER_TOKEN` + `METRICOOL_USER_ID`, network access to `app.metricool.com`; unattended,
   not yet verified against a live account — start in `draft`).
6. Set `publishing.enabled: true` and `mode: draft`; refresh brands; run
   `channel-factory month schedule <YYYY-MM> --channel <id>`; switch to `autopilot` once you trust it.

### Documented limits (re-check before enabling)
Media URLs must be public and non-expiring. TikTok via Metricool: 3 s – 10 min, ≤ 500 MB.
Shorts: project's conservative cap of 175 s. Thumbnails depend on network/account. Capability keys
are internal to Channel Factory, not Metricool fields.
