"""
In-memory DB stand-in so the pipeline is testable with no Postgres, no API keys,
and no network. Used by `scripts/validate.py` and by DRY_RUN runs.

Same interface as pod.db.DB. Nothing persists -- that is the point: it proves
the state machine, the gates, and the guardrails behave, isolated from
infrastructure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .state import Experiment, Stage


class FakeCursor:
    def __init__(self, store: "MemoryDB"):
        self.store = store
        self._rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: Any = None):
        s = " ".join(sql.split()).lower()
        params = params or ()
        self._rows = []

        if s.startswith("select count(*) as n from experiments where stage=%s"):
            stage = params[0]
            self._rows = [{"n": sum(1 for e in self.store.exps.values() if e.stage.value == stage)}]
        elif "from experiments where stage not in" in s:
            self._rows = [
                {"data": _dump(e)} for e in self.store.exps.values()
                if e.stage not in (Stage.KILLED, Stage.HALTED)
            ]
        elif s.startswith("select data from experiments where stage=%s"):
            stage = params[0]
            self._rows = [
                {"data": _dump(e)} for e in self.store.exps.values()
                if e.stage.value == stage
            ]
        elif s.startswith("select data from experiments where id=%s"):
            e = self.store.exps.get(params[0])
            self._rows = [{"data": _dump(e)}] if e else []
        elif "where lower(concept)=lower(%s)" in s:
            needle = str(params[0]).strip().lower()
            hit = any(e.concept.strip().lower() == needle for e in self.store.exps.values())
            self._rows = [{"1": 1}] if hit else []
        elif s.startswith("select count(*) as n from experiments where listed_at::date"):
            today = datetime.now(timezone.utc).date()
            n = sum(1 for e in self.store.exps.values()
                    if e.listed_at and e.listed_at.date() == today)
            self._rows = [{"n": n}]
        elif "from events where type='ip_strike'" in s:
            self._rows = [{"n": self.store.ip_strikes}]
        elif "from spend where kind=%s and at > now()" in s:
            self._rows = [{"s": self.store._spend_week.get(params[0], 0.0)}]
        elif "from spend where kind=%s and at::date = current_date" in s:
            self._rows = [{"s": self.store._spend_today.get(params[0], 0.0)}]
        elif "from spend where at > now()" in s:
            self._rows = [{"s": sum(self.store._spend_week.values())}]
        elif "from sales where at > now()" in s:
            self._rows = [{"sold": self.store.sold, "refunded": self.store.refunded}]
        elif s.startswith("select stage, count(*)"):
            counts: dict[str, int] = {}
            for e in self.store.exps.values():
                counts[e.stage.value] = counts.get(e.stage.value, 0) + 1
            self._rows = [{"stage": k, "n": v} for k, v in counts.items()]
        elif "split_part(kill_reason" in s:
            gates: dict[str, int] = {}
            for e in self.store.exps.values():
                if e.stage is Stage.KILLED and e.kill_reason:
                    g = e.kill_reason.split(":")[0]
                    gates[g] = gates.get(g, 0) + 1
            self._rows = [{"gate": k, "n": v} for k, v in
                          sorted(gates.items(), key=lambda kv: -kv[1])[:8]]
        elif s.startswith("select coalesce(sum(gross),0)"):
            self._rows = [{
                "revenue": self.store.revenue, "cogs": self.store.cogs,
                "ads": self.store.ads, "selling_designs": self.store.selling,
                "killed": sum(1 for e in self.store.exps.values() if e.stage is Stage.KILLED),
                "scaling": sum(1 for e in self.store.exps.values() if e.stage is Stage.SCALING),
                "total": len(self.store.exps),
            }]
        elif s.startswith("select experiment_id from exp_variations"):
            self._rows = [{"experiment_id": self.store.varmap.get(params[0])}] \
                if params[0] in self.store.varmap else []
        elif s.startswith("select id, type, data, at from events"):
            prefix = params[0].rstrip("%")
            cutoff = datetime.now(timezone.utc) - timedelta(hours=params[1])
            self._rows = [
                {"id": i, "type": t, "data": d, "at": a}
                for i, (t, d, a) in enumerate(self.store.events)
                if t.startswith(prefix) and a > cutoff
            ][: params[2]]
        # Writes: no-ops that succeed.
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    @property
    def rowcount(self):
        return len(self._rows)


class _Conn:
    def __init__(self, store: "MemoryDB"):
        self.store = store
        self.closed = False

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class MemoryDB:
    """Drop-in for pod.db.DB."""

    def __init__(self):
        self.exps: dict[str, Experiment] = {}
        self.varmap: dict[str, str] = {}
        self.events: list[tuple[str, dict, datetime]] = []
        self.sales: list[dict] = []
        self._spend_week: dict[str, float] = {"ads": 0.0, "fulfillment": 0.0}
        self._spend_today: dict[str, float] = {"ads": 0.0, "fulfillment": 0.0}
        self.ip_strikes = 0
        self.revenue = 0.0
        self.cogs = 0.0
        self.ads = 0.0
        self.sold = 0
        self.refunded = 0
        self.selling = 0
        self._conn = _Conn(self)

    @property
    def conn(self):
        return self._conn

    def close(self):
        self._conn.close()

    # -- experiments -------------------------------------------------------
    def insert_experiment(self, exp: Experiment) -> str:
        self.exps[exp.id] = exp
        return exp.id

    def save(self, exp: Experiment) -> None:
        exp.updated_at = datetime.now(timezone.utc).isoformat()
        self.exps[exp.id] = exp

    def get(self, exp_id: str) -> Experiment | None:
        return self.exps.get(exp_id)

    def by_stage(self, stage: Stage, limit: int = 200) -> list[Experiment]:
        return [e for e in self.exps.values() if e.stage is stage][:limit]

    def active(self, limit: int = 500) -> list[Experiment]:
        return [e for e in self.exps.values()
                if e.stage not in (Stage.KILLED, Stage.HALTED)][:limit]

    def count_staged(self, stage: Stage) -> int:
        return sum(1 for e in self.exps.values() if e.stage is stage)

    def concept_exists(self, concept: str) -> bool:
        needle = concept.strip().lower()
        return any(e.concept.strip().lower() == needle for e in self.exps.values())

    # -- mapping -----------------------------------------------------------
    def map_variations(self, exp_id: str, variation_ids: list[str], skus: list[str]) -> None:
        for vid in variation_ids:
            self.varmap[vid] = exp_id

    def experiment_for_variation(self, variation_id: str) -> str | None:
        return self.varmap.get(variation_id)

    # -- metrics -----------------------------------------------------------
    def record_sale(self, exp_id, variation_id, qty, gross, order_id) -> None:
        self.sales.append({"exp": exp_id, "vid": variation_id, "qty": qty,
                           "gross": gross, "order": order_id, "refunded": False})
        self.revenue += gross
        self.sold += qty
        self.selling = len({s["exp"] for s in self.sales})
        self.refresh_metrics(exp_id)

    def record_refund_by_order(self, order_id: str) -> None:
        touched: set[str] = set()
        for s in self.sales:
            if s["order"] == order_id and not s["refunded"]:
                s["refunded"] = True
                self.refunded += s["qty"]
                touched.add(s["exp"])
        # refresh_metrics is keyed by EXPERIMENT id, not order id. Refresh every
        # experiment this order touched -- an order can span several designs.
        for exp_id in touched:
            self.refresh_metrics(exp_id)

    def refresh_metrics(self, exp_id: str) -> None:
        exp = self.exps.get(exp_id)
        if not exp:
            return
        rows = [s for s in self.sales if s["exp"] == exp_id]
        exp.orders = sum(s["qty"] for s in rows)
        exp.revenue = sum(s["gross"] for s in rows)
        exp.refunds = sum(s["qty"] for s in rows if s["refunded"])

    def record_traffic(self, exp_id, impressions=0, clicks=0, atc=0) -> None:
        exp = self.exps.get(exp_id)
        if exp:
            exp.impressions += impressions
            exp.clicks += clicks
            exp.add_to_cart += atc

    def record_ad_spend(self, exp_id, amount) -> None:
        self.ads += amount
        self._spend_week["ads"] += amount
        self._spend_today["ads"] += amount
        exp = self.exps.get(exp_id)
        if exp:
            exp.ad_spend += amount

    def record_fulfillment_spend(self, amount, order_id="") -> None:
        self.cogs += amount
        self._spend_week["fulfillment"] += amount
        self._spend_today["fulfillment"] += amount

    # -- guardrail inputs --------------------------------------------------
    def spend_this_week(self, kind=None) -> float:
        if kind:
            return self._spend_week.get(kind, 0.0)
        return sum(self._spend_week.values())

    def spend_today(self, kind="ads") -> float:
        return self._spend_today.get(kind, 0.0)

    def listed_today(self) -> int:
        today = datetime.now(timezone.utc).date()
        return sum(1 for e in self.exps.values() if e.listed_at and e.listed_at.date() == today)

    def ip_strike_count(self) -> int:
        return self.ip_strikes

    def increment_ops_alert(self, kind, ref) -> None:
        self.log_event(f"ops_alert:{kind}", {"ref": ref})

    def log_event(self, type_, data) -> int:
        self.events.append((type_, data, datetime.now(timezone.utc)))
        if type_ == "ip_strike":
            self.ip_strikes += 1
        return len(self.events)

    def recent_events(self, type_prefix, hours=24, limit=50) -> list[dict]:
        prefix = type_prefix
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        out = [
            {"type": t, "data": d, "at": a}
            for (t, d, a) in self.events
            if t.startswith(prefix) and a > cutoff
        ]
        return out[:limit]

    def pnl_snapshot(self) -> dict:
        fees = self.revenue * 0.033
        return {
            "revenue": round(self.revenue, 2), "cogs": round(self.cogs, 2),
            "ads": round(self.ads, 2), "square_fees_est": round(fees, 2),
            "net": round(self.revenue - self.cogs - self.ads - fees, 2),
            "selling_designs": self.selling,
            "killed": sum(1 for e in self.exps.values() if e.stage is Stage.KILLED),
            "scaling": sum(1 for e in self.exps.values() if e.stage is Stage.SCALING),
            "total": len(self.exps),
        }


def _dump(exp: Experiment) -> dict:
    from .db import _serialize
    return _serialize(exp)
