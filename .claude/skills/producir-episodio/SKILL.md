---
name: producir-episodio
description: Produce un episodio largo en inglés (6–10 min) para un canal configurado — investigación con claims, guion por escenas, dirección visual, voz, render, QA y revisión. Úsala cuando el usuario pida un episodio o video concreto.
argument-hint: <id-de-canal> <tema>
---

# Producir un episodio

Argumentos: `$ARGUMENTS`. Sigue MASTER_PROMPT.md §6–§9 y la biblia del canal (`channel-factory` lee el perfil).

1. **Comprobaciones**: `channel-factory doctor --no-network`; el canal debe tener voz (`voices select` o
   provisional) y biblia. Si falta algo, una sola ronda de preguntas.
2. **Esqueleto**: `channel-factory episode new <ep-id> --channel <id> --title "<working title>"`.
3. **Guion** → subagente `guionista`: `episode.yaml` (escenas con beats) + `claims.json` con fuentes primarias.
4. **Visual** → subagente `director-visual`: assets en `assets/`, cámara, overlays con `at_text`, `vertical_roi`,
   `vertical_layout` si hace falta, y estilo (imágenes propias o MP4 de una skill de `docs/styles.md`).
5. **Validar**: `channel-factory episode check <ep-id> --channel <id>` hasta `ok: true`.
6. **Render**: `channel-factory episode render <ep-id> --channel <id> --vertical-preview`.
7. **Revisión** → subagente `revisor`: QA técnica + muestreo de fotogramas y audio + claims + identidad.
   Corrige solo las escenas falladas y vuelve a renderizar (la caché reutiliza el resto).
8. **Clips** (si Clip Master está configurado) → subagente `editor-clips`.
9. Informa: rutas de `render/`, duración, problemas resueltos y pendientes. No publiques (Metricool desactivado).
