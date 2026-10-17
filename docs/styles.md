# Estilos visuales con skills de Claude

Channel Factory monta cada episodio por escenas. Además de imágenes y capas propias, una escena puede
usar como fondo un **MP4 generado por una skill de estilo**: el compositor aplica encima cámara,
personajes, textos y gráficos exactos, y la narración/mezcla/QA siguen siendo las del pipeline.

```yaml
# episode.yaml → escena
visual:
  video: {path: assets/s04_hyperframes.mp4, loop: false, style: hyperframes}
  camera: {from: [0.5, 0.5, 1.0], to: [0.5, 0.5, 1.05]}
  overlays: [{type: text, text: "24% in five years", at_text: "24%"}]
```

| Skill | Licencia | Uso comercial | Pago | Mejor para | Estado |
|---|---|---|---|---|---|
| [HyperFrames](https://github.com/heygen-com/hyperframes) | Apache-2.0 | Sí | No | Títulos, datos animados, listas, overlays | Opcional, sin probar |
| [onetake](https://github.com/feitangyuan/onetake) | PolyForm Noncommercial | **No** | No | Demos continuas de producto | Bloqueada por licencia |
| [Collage B-roll](https://github.com/pyang5166/gbro-collage-broll) | MIT | Sí | **Sí (Gemini)** | Metáforas de ~5 s | Necesita API key + presupuesto |
| [Claude Animation](https://github.com/JohnHeibel/ClaudeAnimationBase) | MIT | Sí | No | Personaje animado a mano | Opcional, sin probar |
| [Lemo-Opuscar](https://github.com/lemomo-ai/lemo-opuscar) | MIT | Sí | No | ~40 estilos de cine en código | Opcional, sin probar |

Registro máquina: [`providers/styles.yaml`](../providers/styles.yaml).

## Cómo se usan (agente `director-visual`)
1. Elige el estilo por escena según la biblia del canal (no mezclar cinco estilos en un episodio:
   uno principal y como mucho uno de acento).
2. Comprueba licencia y coste: nada con `commercial_use: false` en canales monetizados; lo de pago
   solo con presupuesto (`budget.paid_providers_enabled` y topes).
3. Genera el plano con la skill (sin audio o descartándolo), 1920×1080, la duración de la escena.
4. Guarda el MP4 en `assets/` del episodio y referencia `visual.video` en la escena.
5. `channel-factory episode check` y `episode render`; revisa fotogramas en los límites de escena.

Requisitos comunes: Node, FFmpeg y Chrome/Chromium. Ninguna se instala automáticamente.
