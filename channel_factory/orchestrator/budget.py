"""Presupuesto con reservas atómicas por episodio, lote y mes.

Una etapa pagada reserva su coste estimado antes de llamar al proveedor;
al terminar registra el gasto real (`commit`) o libera la reserva. Los topes
se comprueban dentro de BEGIN IMMEDIATE contando cargos + reservas activas,
así que dos workers concurrentes no pueden sobrepasar el límite.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import DB, new_id, utcnow_iso

CATEGORIES = ("llm", "image", "video", "tts", "storage", "publishing", "other")


class BudgetBlocked(RuntimeError):
    """La etapa pagada no puede ejecutarse con la configuración/presupuesto actual."""


@dataclass
class Caps:
    paid_enabled: bool
    episode: float | None
    batch: float | None
    month: float | None


class Budget:
    def __init__(self, db: DB, caps: Caps):
        self.db = db
        self.caps = caps

    def _spent(self, c, column: str, value: str | None) -> float:  # noqa: ANN001
        if value is None:
            return 0.0
        r = c.execute(
            f"SELECT COALESCE(SUM(amount_usd),0) AS s FROM budget_ledger WHERE {column}=? AND status IN ('reserved','charged')",
            (value,),
        ).fetchone()
        return float(r["s"])

    def reserve(
        self,
        amount_usd: float,
        *,
        provider: str,
        category: str,
        channel_id: str | None = None,
        batch_id: str | None = None,
        episode_id: str | None = None,
        month: str | None = None,
        job_id: str | None = None,
        note: str | None = None,
    ) -> str | None:
        """Devuelve el id de la reserva, o None si el coste es 0 (proveedor gratuito)."""
        if category not in CATEGORIES:
            raise ValueError(f"categoría desconocida: {category}")
        if amount_usd <= 0:
            return None
        if not self.caps.paid_enabled:
            raise BudgetBlocked(
                f"{provider}: proveedor de pago bloqueado (budget.paid_providers_enabled=false). "
                "Configura un presupuesto o usa un proveedor gratuito."
            )
        with self.db.tx() as c:
            for column, value, cap, label in (
                ("episode_id", episode_id, self.caps.episode, "episodio"),
                ("batch_id", batch_id, self.caps.batch, "lote"),
                ("month", month, self.caps.month, "mes"),
            ):
                if value is None:
                    continue
                if cap is None:
                    raise BudgetBlocked(f"No hay tope de {label} configurado: no se gasta sin límite.")
                spent = self._spent(c, column, value)
                if spent + amount_usd > cap + 1e-9:
                    raise BudgetBlocked(
                        f"Tope de {label} {cap:.2f} USD: gastado/reservado {spent:.2f}, se piden {amount_usd:.2f}."
                    )
            if episode_id is None and batch_id is None and month is None:
                raise BudgetBlocked("Una reserva pagada debe imputarse a episodio, lote o mes.")
            entry = new_id("bud")
            now = utcnow_iso()
            c.execute(
                """INSERT INTO budget_ledger(entry_id, channel_id, batch_id, episode_id, month, provider, category,
                       kind, amount_usd, status, job_id, note, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,'reservation',?,'reserved',?,?,?,?)""",
                (entry, channel_id, batch_id, episode_id, month, provider, category, amount_usd, job_id, note, now, now),
            )
            return entry

    def commit(self, reservation_id: str | None, actual_usd: float) -> None:
        if reservation_id is None:
            return
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM budget_ledger WHERE entry_id=?", (reservation_id,)).fetchone()
            if not r or r["status"] != "reserved":
                raise ValueError(f"reserva no activa: {reservation_id}")
            c.execute(
                "UPDATE budget_ledger SET status='charged', kind='charge', note=COALESCE(note,'')||?, amount_usd=?, updated_at=? WHERE entry_id=?",
                (f" | estimado {r['amount_usd']:.4f}", actual_usd, utcnow_iso(), reservation_id),
            )

    def release(self, reservation_id: str | None) -> None:
        if reservation_id is None:
            return
        with self.db.tx() as c:
            c.execute(
                "UPDATE budget_ledger SET status='released', updated_at=? WHERE entry_id=? AND status='reserved'",
                (utcnow_iso(), reservation_id),
            )

    def report(self, **where: str) -> dict:
        sql, params = "SELECT category, status, SUM(amount_usd) AS s FROM budget_ledger WHERE 1=1", []
        for k, v in where.items():
            if v is not None:
                sql += f" AND {k}=?"
                params.append(v)
        out: dict = {}
        for r in self.db.query(sql + " GROUP BY category, status", tuple(params)):
            out.setdefault(r["category"], {})[r["status"]] = round(r["s"], 4)
        return out


def caps_from_settings(settings, channel_id: str | None = None) -> Caps:  # noqa: ANN001
    b = settings.budget(channel_id)
    return Caps(b.paid_providers_enabled, b.episode_cap_usd, b.batch_cap_usd, b.monthly_cap_usd)
