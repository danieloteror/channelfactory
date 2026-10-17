"""Publicación en Metricool: capacidades, payload, validación, ledger, transportes y almacenamiento.

DESACTIVADA por defecto (`publishing.enabled: false`): con ella apagada nada se envía y los
episodios quedan listos para subir a mano. Recomendada para automatizar (docs/metricool.md).

Flujo: `prepare` (outbox con clave de deduplicación) → `validate` (bloquea antes de
enviar) → `submit` (lock + transporte) → `record_*` → `reconcile` (estado remoto).
`scheduled` (aceptado por Metricool) NO significa `published` (visible en la red).
Ver docs/metricool.md.
"""
