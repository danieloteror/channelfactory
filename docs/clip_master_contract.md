# Contrato Channel Factory ↔ Clip Master (v1.0)

Clip Master se usa como **agente de recortes**: proceso/repositorio independiente que recibe un
master horizontal y devuelve clips verticales. El adaptador **no** modifica su núcleo, **no** escribe
en su configuración (el perfil `original_explainer` viaja dentro de cada solicitud) y **no** activa
publicación: `allow_publish` es siempre `false` (validado en config y en el modelo). Channel Factory
es el único propietario de la publicación.

## Qué falta del propietario
- Ruta o repositorio de Clip Master y cómo se invoca hoy (CLI/API).
- Su formato de entrada y salida actual, para ajustar el contrato (las rutas de ejemplo no existen).

Configuración (`config/factory.yaml` o `config/factory.local.yaml`):
```yaml
clip_master:
  path: /ruta/a/clip-master
  transport: cli            # o http (http_url)
  command: ["python", "-m", "clipmaster", "--request", "{request}", "--out", "{out_dir}"]
  profile: original_explainer
  allow_publish: false
```
El comando solo sale de este archivo; nunca de contenido de terceros.

## Solicitud (`ClipRequest`, esquema en `docs/schemas/clip_request.schema.json`)
| Campo | Significado |
|---|---|
| `job_id`, `channel_id`, `episode_id` | Identidad y deduplicación |
| `source_video`, `source_checksum` | Master limpio (sin subtítulos incrustados) y su SHA-256 |
| `source_type` | `original_explainer` (no aplica criterios de gol/jugada/gameplay) |
| `transcript_path` | Segmentos por escena con tiempos reales de la narración |
| `scene_manifest_path` | Escenas con inicio/fin, beat, `clip_hint`, `vertical_roi`, versión de guion |
| `preferred_duration_seconds`, `max_duration_seconds` | 65–120 s preferido; máximo 175 s |
| `max_clips` | Hasta 3; menos si no hay unidades buenas (con explicación) |
| `candidate_hints` | Unidades narrativas sugeridas por la estructura del guion (pregunta → resolución) |
| `allow_publish` | Siempre `false` |

## Resultado (`ClipResult`, esquema en `docs/schemas/clip_result.schema.json`)
Por clip: archivo, checksum, tiempos de origen, versión de guion, transcripción, hook, justificación,
puntuaciones desglosadas, subtítulos, miniatura opcional y reporte de calidad. `explanation` es
obligatoria si se entregan menos clips de los pedidos. Nunca se acepta un «clip generado» a secas.

## Validación del adaptador (`clip_master_adapter/contract.py`)
Rechaza: archivo inexistente o checksum distinto, sin video/audio, no 9:16, duración fuera de rango,
tiempos fuera del master, campos vacíos, apertura con pronombre/conector huérfano, versión de guion
distinta, clips que se solapan > 50 % (mismo fragmento con otro título) y falta de explicación.

## Reencuadre vertical
Primero recorte por `vertical_roi` de cada escena; si corta al personaje o a un gráfico esencial, el
compositor recompone la escena con los mismos assets (`vertical_layout`). Si un clip necesita una
apertura nueva, se genera solo esa pieza. El mismo MP4 vertical sirve para Shorts y TikTok; los
metadatos se separan por destino.
