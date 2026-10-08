"""
Webhook receivers for Square and Printful.

Runs as a small FastAPI service inside the OpenBot deployment (or on the same
host). Two jobs:

  1. Attribute sales to experiments. Square sends order.created /
     payment.updated; we pull line items, read the catalog_variation_id, map it
     back to an experiment via the `exp_variations` table, and increment the
     counters the test gate reads. Without this the TESTING stage has no data
     and nothing can ever be promoted or killed on evidence.

  2. Detect the failure modes that are invisible in a dashboard. Printful
     orders stuck in NEEDS_APPROVAL (customer paid, nothing printing),
     fulfillment errors, refunds.

Security: verify Square's HMAC signature on the RAW body. An unverified public
webhook endpoint is an invitation to fabricate your sales data, which in a
fully autonomous system means an attacker can make the bot scale a design by
sending it fake order events.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .config import CREDS
from .db import DB
from .square import verify_square_signature

log = logging.getLogger("pod.webhooks")

app = FastAPI(title="pod-autopilot webhooks", version="1.0")
db = DB()


def _public_url() -> str:
    """Must match, byte for byte, the notification_url in the Square
    subscription. Signatures are computed over this string, so a trailing
    slash difference breaks every single webhook."""
    return os.environ.get("PUBLIC_WEBHOOK_URL", "https://example.com/hooks/square")


@app.post("/hooks/square")
async def square_hook(
    request: Request,
    x_square_hmacsha256_signature: str | None = Header(default=None),
    x_square_request_id: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = await request.body()

    if not verify_square_signature(
        raw, _public_url(), CREDS.webhook_secret, x_square_hmacsha256_signature or ""
    ):
        # Log it. Repeated failures mean either a wrong secret or someone
        # probing the endpoint -- both worth knowing about.
        log.warning("Square webhook signature verification FAILED")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = __import__("json").loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="malformed json") from exc

    event_type = payload.get("type", "")
    data = payload.get("data", {})
    obj = data.get("object", {})

    # Square retries on non-2xx. Acknowledge fast, process async in real life.
    # Returning 500 here for a processing bug causes an event storm.
    try:
        if event_type == "order.updated":
            _handle_order(obj)
        elif event_type == "order.created":
            _handle_order(obj)
        elif event_type == "payment.updated":
            _handle_payment(obj)
        elif event_type in ("refund.created", "refund.updated"):
            _handle_refund(obj)
        elif event_type == "catalog.version_updated":
            db.log_event("catalog_version_updated", {"at": _now()})
    except Exception:  # noqa: BLE001
        log.exception("Error handling %s -- acknowledged anyway", event_type)

    return {"ok": True, "type": event_type}


def _handle_order(order: dict) -> None:
    """Attribute line items to experiments."""
    order_id = order.get("id")
    state = order.get("state")
    if state not in ("OPEN", "COMPLETED"):
        return
    for line in order.get("line_items", []) or []:
        vid = line.get("catalog_variation_id")
        if not vid:
            continue
        qty = int(line.get("quantity", 0) or 0)
        gross = int(line.get("gross_sales_money", {}).get("amount", 0) or 0) / 100.0
        exp_id = db.experiment_for_variation(vid)
        if not exp_id:
            log.info("Sale on variation %s not mapped to any experiment", vid)
            continue
        db.record_sale(exp_id, vid, qty, gross, order_id)
        log.info("Attributed %d unit(s) $%.2f to %s", qty, gross, exp_id)


def _handle_payment(payment: dict) -> None:
    status = payment.get("status")
    if status != "COMPLETED":
        return
    db.log_event(
        "payment_completed",
        {
            "payment_id": payment.get("id"),
            "amount": payment.get("amount_money", {}).get("amount"),
            "order_id": payment.get("order_id"),
        },
    )


def _handle_refund(refund: dict) -> None:
    """Refunds feed the refund-rate guardrail. A design that sells and gets
    returned 15% of the time is a quality or listing-honesty problem, and
    scaling it makes that worse."""
    db.log_event(
        "refund",
        {
            "refund_id": refund.get("id"),
            "status": refund.get("status"),
            "amount": refund.get("amount_money", {}).get("amount"),
            "order_id": refund.get("order_id"),
            "reason": refund.get("reason"),
        },
    )
    order_id = refund.get("order_id")
    if order_id and refund.get("status") in ("COMPLETED", "PENDING"):
        db.record_refund_by_order(order_id)


# ---------------------------------------------------------------------------
# Printful webhooks. Configure in Printful Dashboard -> Settings -> Webhooks.
# Printful does NOT sign webhooks with HMAC by default, so protect this
# endpoint with a shared secret in the path.
# ---------------------------------------------------------------------------

PRINTFUL_HOOK_SECRET = os.environ.get("PRINTFUL_HOOK_SECRET", "")


@app.post("/hooks/printful/{secret}")
async def printful_hook(secret: str, request: Request) -> dict[str, Any]:
    if not PRINTFUL_HOOK_SECRET or secret != PRINTFUL_HOOK_SECRET:
        raise HTTPException(status_code=404, detail="not found")
    payload = await request.json()
    ptype = payload.get("type", "")
    data = payload.get("data", {})

    # Events that matter:
    #   order_imported / order_synced -- order reached Printful
    #   order_failed                  -- CANNOT FULFILL. Money taken, no product.
    #   package_shipped               -- tracking available
    #   order_canceled / order_refunded
    #   product_synced                -- our listing landed in the store
    if ptype in ("order_failed", "order_canceled", "order_refunded"):
        log.error("Printful %s: %s", ptype, data)
        db.log_event(f"printful_{ptype}", data)
        db.increment_ops_alert(ptype, str(data.get("order_id", "")))
    elif ptype == "package_shipped":
        db.log_event("printful_shipped", {"order_id": data.get("order_id")})
    else:
        db.log_event(f"printful_{ptype}", {"order_id": data.get("order_id")})

    return {"ok": True}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "time": _now()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
