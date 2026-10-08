"""
Postgres-backed store for experiments.

Uses the OpenBot deployment's Postgres (which already runs pgvector) on a
separate database or schema. Keeping pipeline state next to the agent's audit
log means one backup covers both.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

import os

from .config import CREDS
from .state import Experiment, Signal, Stage

log = logging.getLogger("pod.db")


class DB:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or CREDS.db_url
        self._conn: psycopg.Connection | None = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    # -- experiments -------------------------------------------------------

    def insert_experiment(self, exp: Experiment) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO experiments
                  (id, stage, niche, concept, keywords, audience, data,
                   demand_score, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data,
                  stage = EXCLUDED.stage, updated_at = EXCLUDED.updated_at
                """,
                (
                    exp.id, exp.stage.value, exp.niche, exp.concept,
                    json.dumps(exp.keywords), exp.audience,
                    json.dumps(_serialize(exp)), exp.demand_score,
                    _ts(exp.created_at), _ts(exp.updated_at),
                ),
            )
        self.conn.commit()
        return exp.id

    def save(self, exp: Experiment) -> None:
        exp.updated_at = datetime.now(timezone.utc).isoformat()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE experiments
                   SET stage=%s, demand_score=%s, data=%s, updated_at=%s
                 WHERE id=%s
                """,
                (exp.stage.value, exp.demand_score, json.dumps(_serialize(exp)),
                 _ts(exp.updated_at), exp.id),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                self.insert_experiment(exp)
                return
        self.conn.commit()

    def get(self, exp_id: str) -> Experiment | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM experiments WHERE id=%s", (exp_id,))
            row = cur.fetchone()
        return _deserialize(row["data"]) if row else None

    def by_stage(self, stage: Stage, limit: int = 200) -> list[Experiment]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM experiments WHERE stage=%s ORDER BY updated_at DESC LIMIT %s",
                (stage.value, limit),
            )
            rows = cur.fetchall()
        return [_deserialize(r["data"]) for r in rows]

    def active(self, limit: int = 500) -> list[Experiment]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT data FROM experiments
                    WHERE stage NOT IN ('KILLED','HALTED')
                    ORDER BY updated_at DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
        return [_deserialize(r["data"]) for r in rows]

    def count_staged(self, stage: Stage) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM experiments WHERE stage=%s", (stage.value,)
            )
            return int(cur.fetchone()["n"])

    def concept_exists(self, concept: str) -> bool:
        """Deduplicate. An autonomous loop will rediscover the same trend every
        single day and rebuild the same shirt unless you stop it."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM experiments WHERE lower(concept)=lower(%s) LIMIT 1",
                (concept.strip(),),
            )
            return cur.fetchone() is not None

    # -- variation -> experiment mapping ----------------------------------

    def map_variations(self, exp_id: str, variation_ids: list[str], skus: list[str]) -> None:
        with self.conn.cursor() as cur:
            for vid, sku in zip(variation_ids, skus):
                cur.execute(
                    """INSERT INTO exp_variations (experiment_id, square_variation_id, sku)
                       VALUES (%s,%s,%s)
                       ON CONFLICT (square_variation_id) DO UPDATE SET experiment_id=EXCLUDED.experiment_id""",
                    (exp_id, vid, sku),
                )
        self.conn.commit()

    def experiment_for_variation(self, variation_id: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT experiment_id FROM exp_variations WHERE square_variation_id=%s",
                (variation_id,),
            )
            row = cur.fetchone()
        return row["experiment_id"] if row else None

    # -- metrics -----------------------------------------------------------

    def record_sale(self, exp_id: str, variation_id: str, qty: int, gross: float, order_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sales (experiment_id, variation_id, order_id, qty, gross, at)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (order_id, variation_id) DO NOTHING""",
                (exp_id, variation_id, order_id, qty, gross, datetime.now(timezone.utc)),
            )
        self.conn.commit()
        self.refresh_metrics(exp_id)

    def record_refund_by_order(self, order_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE sales SET refunded=true WHERE order_id=%s RETURNING experiment_id",
                (order_id,),
            )
            rows = cur.fetchall()
        self.conn.commit()
        for r in rows:
            self.refresh_metrics(r["experiment_id"])

    def refresh_metrics(self, exp_id: str) -> None:
        """Recompute counters from the sales table rather than incrementing.
        Incrementing drifts on retries and duplicate webhooks; recomputing is
        idempotent."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(qty),0) AS orders,
                          COALESCE(SUM(gross),0) AS revenue,
                          COALESCE(SUM(qty) FILTER (WHERE refunded),0) AS refunds
                     FROM sales WHERE experiment_id=%s""",
                (exp_id,),
            )
            row = cur.fetchone()
            cur.execute(
                """UPDATE experiments
                      SET orders=%s, revenue=%s, refunds=%s, updated_at=%s
                    WHERE id=%s""",
                (int(row["orders"]), float(row["revenue"]), int(row["refunds"]),
                 datetime.now(timezone.utc), exp_id),
            )
        self.conn.commit()

    def record_traffic(self, exp_id: str, impressions: int = 0, clicks: int = 0, atc: int = 0) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE experiments
                      SET impressions=impressions+%s, clicks=clicks+%s,
                          add_to_cart=add_to_cart+%s, updated_at=%s
                    WHERE id=%s""",
                (impressions, clicks, atc, datetime.now(timezone.utc), exp_id),
            )
        self.conn.commit()

    def record_ad_spend(self, exp_id: str, amount: float) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE experiments SET ad_spend=ad_spend+%s, updated_at=%s WHERE id=%s",
                (amount, datetime.now(timezone.utc), exp_id),
            )
            cur.execute(
                "INSERT INTO spend (experiment_id, amount, kind, at) VALUES (%s,%s,'ads',%s)",
                (exp_id, amount, datetime.now(timezone.utc)),
            )
        self.conn.commit()

    def record_fulfillment_spend(self, amount: float, order_id: str = "") -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO spend (experiment_id, amount, kind, ref, at) VALUES (NULL,%s,'fulfillment',%s,%s)",
                (amount, order_id, datetime.now(timezone.utc)),
            )
        self.conn.commit()

    # -- guardrail inputs --------------------------------------------------

    def spend_this_week(self, kind: str | None = None) -> float:
        with self.conn.cursor() as cur:
            if kind:
                cur.execute(
                    "SELECT COALESCE(SUM(amount),0) AS s FROM spend WHERE kind=%s AND at > now() - interval '7 days'",
                    (kind,),
                )
            else:
                cur.execute(
                    "SELECT COALESCE(SUM(amount),0) AS s FROM spend WHERE at > now() - interval '7 days'"
                )
            return float(cur.fetchone()["s"])

    def spend_today(self, kind: str = "ads") -> float:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM spend WHERE kind=%s AND at::date = current_date",
                (kind,),
            )
            return float(cur.fetchone()["s"])

    def listed_today(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM experiments WHERE listed_at::date = current_date"
            )
            return int(cur.fetchone()["n"])

    def ip_strike_count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM events WHERE type='ip_strike'")
            return int(cur.fetchone()["n"])

    def increment_ops_alert(self, kind: str, ref: str) -> None:
        self.log_event(f"ops_alert:{kind}", {"ref": ref})

    def log_event(self, type_: str, data: dict) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (type, data, at) VALUES (%s,%s,%s) RETURNING id",
                (type_, json.dumps(data, default=str), datetime.now(timezone.utc)),
            )
            row = cur.fetchone()
        self.conn.commit()
        return int(row["id"])

    def recent_events(self, type_prefix: str, hours: int = 24, limit: int = 50) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT id, type, data, at FROM events
                    WHERE type LIKE %s AND at > now() - make_interval(hours => %s)
                    ORDER BY at DESC LIMIT %s""",
                (f"{type_prefix}%", hours, limit),
            )
            return cur.fetchall()

    def pnl_snapshot(self) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT
                     (SELECT COALESCE(SUM(gross),0) FROM sales) AS revenue,
                     (SELECT COALESCE(SUM(amount),0) FROM spend WHERE kind='fulfillment') AS cogs,
                     (SELECT COALESCE(SUM(amount),0) FROM spend WHERE kind='ads') AS ads,
                     (SELECT count(DISTINCT experiment_id) FROM sales) AS selling_designs,
                     (SELECT count(*) FROM experiments WHERE stage='KILLED') AS killed,
                     (SELECT count(*) FROM experiments WHERE stage='SCALING') AS scaling,
                     (SELECT count(*) FROM experiments) AS total
                """
            )
            row = cur.fetchone()
        rev, cogs, ads = row["revenue"], row["cogs"], row["ads"]
        # Square fees, free plan: 3.3% + $0.30 per transaction.
        fees = rev * 0.033
        row["square_fees_est"] = round(fees, 2)
        row["net"] = round(rev - cogs - ads - fees, 2)
        return row


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _serialize(exp: Experiment) -> dict:
    d = exp.__dict__.copy()
    d["stage"] = exp.stage.value
    d["signals"] = [s.to_dict() for s in exp.signals]
    d["listed_at"] = exp.listed_at.isoformat() if exp.listed_at else None
    return d


def _deserialize(d: dict[str, Any]) -> Experiment:
    exp = Experiment.__new__(Experiment)
    exp.__dict__.update({k: v for k, v in d.items() if k not in ("stage", "signals", "listed_at")})
    exp.stage = Stage(d["stage"])
    exp.signals = [Signal(**s) for s in d.get("signals", [])]
    exp.listed_at = datetime.fromisoformat(d["listed_at"]) if d.get("listed_at") else None
    return exp


def open_db():
    """
    Return a DB, or an in-memory stand-in for offline demo runs.

    POD_MEMORY_DB=1 forces the memory store. Otherwise we try Postgres; if the
    connection fails DURING DRY_RUN we fall back to memory with a loud warning,
    because the point of a dry run is to exercise the pipeline without
    infrastructure. Outside DRY_RUN a missing database is a hard error -- an
    autonomous pipeline that silently loses its state mid-flight is worse than
    one that refuses to start.
    """
    if os.environ.get("POD_MEMORY_DB") == "1":
        from .memory_db import MemoryDB
        log.warning("Using IN-MEMORY store (POD_MEMORY_DB=1). Nothing persists.")
        return MemoryDB()
    try:
        db = DB()
        _ = db.conn
        return db
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("DRY_RUN") or os.environ.get("POD_FAKE_SIGNALS"):
            log.warning(
                "Postgres unreachable (%s). Falling back to IN-MEMORY store "
                "because DRY_RUN/POD_FAKE_SIGNALS is set. This is demo mode; "
                "nothing persists.", exc,
            )
            from .memory_db import MemoryDB
            return MemoryDB()
        raise
