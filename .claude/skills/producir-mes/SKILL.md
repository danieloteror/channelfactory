---
name: producir-mes
description: Planifica y produce el lote mensual de un canal (p. ej. «hazme los videos de noviembre») con cola durable, presupuesto y revisión. Úsala para pedidos de varios episodios o de un mes.
argument-hint: <id-de-canal> <YYYY-MM> [n.º de videos]
---

# Producir un mes

Argumentos: `$ARGUMENTS`. Sigue MASTER_PROMPT.md §13–§14.

1. Resuelve canal, mes y zona horaria. Si hay varios canales activos y no se indicó uno, pregunta; nunca produzcas para todos.
2. Asegura temas en `channels/<id>/backlog.yaml` (usa el análisis de referencias y `published_topics.yaml`).
3. `channel-factory plan-month <YYYY-MM> --channel <id> [--long-videos N]` (idempotente: repetirlo devuelve el mismo lote).
   Presenta el calendario con variedad de pilares y una estimación de horas/coste basada en tiempos medidos.
4. Para cada episodio del lote: guionista → director-visual → `episode check` (ver `/producir-episodio`, pasos 3–5).
5. `channel-factory produce-month <YYYY-MM> --channel <id>` y lanza `channel-factory worker` (proceso persistente;
   en el Mac, `caffeinate -i channel-factory worker`).
6. Seguimiento: `channel-factory status --channel <id>`; los `blocked` dicen qué falta → resuélvelo →
   `channel-factory resume <batch_id>`.
7. Revisión de cada master aprobado por QA (subagente `revisor`) y clips (subagente `editor-clips`).
8. Resumen final: episodios listos, rutas, errores, pendientes. No programes ni publiques: Metricool está desactivado.
