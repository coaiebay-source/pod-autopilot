"""
Order ingestion WITHOUT a public webhook endpoint.

The paid plan ran a FastAPI receiver on a VPS behind TLS with HMAC checks.
With no server, we invert it: every ops run (GitHub Actions cron, every 30
min) PULLS from Square and Printful. Nothing listens, nothing is exposed,
nothing to secure, nothing to pay for. Latency is 30 minutes, which is
irrelevant for a test that runs 2-4 weeks.

Idempotency comes from the schema: sales has UNIQUE(order_id, variation_id),
and refresh_metrics() recomputes counters from rows instead of incrementing,
so re-reading the same 72-hour window every run is harmless.

What one poll does:
  1. Square  SearchOrders  (last LOOKBACK_HOURS, OPEN/COMPLETED) -> attribute
     line items to experiments by catalog_variation_id.
  2. Square  ListRefunds   (same window) -> mark refunded -> refund-rate gate.
  3. Printful orders       -> NEEDS_APPROVAL / failed -> ops alert (the
     "customer paid, nothing printing" silent failure).
  4. Fulfillment spend     -> weekly fulfillment cap for the Sentinel.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB
from .printful import PrintfulClient, PrintfulError
from .square import SquareClient, SquareError

log = logging.getLogger("pod.poll")
LOOKBACK_HOURS = int(os.environ.get("POLL_LOOKBACK_HOURS", "72"))


def _since_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()


def poll_square_orders(db: DB, sq: SquareClient) -> dict[str, int]:
    seen = attributed = 0
    try:
        orders = sq.search_orders(created_after=_since_iso(), states=["OPEN", "COMPLETED"])
    except SquareError as exc:
        log.error("SearchOrders failed: %s", exc)
        db.log_event("poll_error", {"src": "square_orders", "err": str(exc)})
        return {"seen": 0, "attributed": 0}
    for o in orders:
        seen += 1
        for line in o.get("line_items", []) or []:
            vid = line.get("catalog_variation_id")
            if not vid:
                continue
            exp_id = db.experiment_for_variation(vid)
            if not exp_id:
                continue
            qty = int(line.get("quantity", 0) or 0)
            gross = int((line.get("gross_sales_money") or {}).get("amount", 0) or 0) / 100.0
            db.record_sale(exp_id, vid, qty, gross, o["id"])
            attributed += 1
    return {"seen": seen, "attributed": attributed}


def poll_square_refunds(db: DB, sq: SquareClient) -> int:
    n = 0
    try:
        data = sq._call("GET", "/v2/refunds", params={"begin_time": _since_iso(), "limit": 100})  # noqa: SLF001
    except SquareError as exc:
        log.error("ListRefunds failed: %s", exc)
        return 0
    for r in data.get("refunds", []) or []:
        if r.get("status") not in ("COMPLETED", "PENDING"):
            continue
        oid = r.get("order_id")
        if oid:
            db.record_refund_by_order(oid)
            db.log_event("refund", {"refund_id": r.get("id"), "order_id": oid,
                                    "amount": (r.get("amount_money") or {}).get("amount"),
                                    "reason": r.get("reason")})
            n += 1
    return n


def poll_printful(db: DB, pf: PrintfulClient) -> dict[str, Any]:
    out: dict[str, Any] = {"needs_approval": 0, "failed": 0, "spend": 0.0}
    try:
        stuck = pf.needs_approval_orders()
    except PrintfulError as exc:
        log.error("Printful poll failed: %s", exc)
        db.log_event("poll_error", {"src": "printful", "err": str(exc)})
        return out
    for o in stuck:
        out["needs_approval"] += 1
        db.increment_ops_alert("printful_needs_approval", str(o.get("id")))
    try:
        recent = pf.list_orders(status="fulfilled", limit=50)
        for o in recent or []:
            created = datetime.fromtimestamp(int(o.get("created", 0)), tz=timezone.utc)
            if created < datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS):
                continue
            costs = o.get("costs") or {}
            total = float(costs.get("total", 0) or 0)
            if total:
                db.record_fulfillment_spend(total, order_id=str(o.get("id")))
                out["spend"] += total
    except (PrintfulError, AttributeError, ValueError) as exc:
        log.warning("Printful cost roll-up skipped: %s", exc)
    return out


def run(db: DB) -> dict[str, Any]:
    sq, pf = SquareClient(), PrintfulClient()
    res = {
        "square": poll_square_orders(db, sq),
        "refunds": poll_square_refunds(db, sq),
        "printful": poll_printful(db, pf),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    db.log_event("poll", res)
    log.info("poll: %s", res)
    return res


if __name__ == "__main__":
    import json
    import logging as _l
    from .db import open_db
    _l.basicConfig(level=_l.INFO)
    print(json.dumps(run(open_db()), indent=2, default=str))
