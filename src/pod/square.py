"""
Square client.

READ THIS FIRST -- the constraint that breaks most Square POD automations:

Square's `item_data.ecom_visibility` and `item_data.ecom_available` fields are
RETURNED by the Catalog API but are READ-ONLY. Square staff have confirmed on
their developer forum (thread "How do I update the field ecom_visibility via the
Square API?") that there is no public API to set online-store visibility, and no
roadmap commitment. The legacy `available_online` field predates modern Square
Online and is not reliable.

Consequence: you can create a perfect catalog item via API and it will sit in
the Item Library invisible to buyers. This is why OpenBot's browser is in the
architecture at all -- it is not a novelty, it is the only way to flip that bit
programmatically, short of setting the dashboard default.

Two mitigations, use BOTH:
  1. Set the default once, by hand:
     Square Dashboard -> Online -> Items -> Item Sync ->
     "Item visibility settings" -> Visible
     After that, API-created and Printful-pushed items appear live by default.
     This alone handles ~95% of cases.
  2. Belt-and-braces: the Catalog Bot verifies each new listing is actually
     purchasable by loading the public storefront URL and looking for the item.
     If it is missing, it escalates to the browser step (flip visibility in
     Online -> Items -> Site Items -> <item> -> Site visibility -> Visible ->
     Save). Verification against the live storefront is the ground truth;
     the catalog API response is not.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import time
import uuid
from base64 import b64encode
from typing import Any

import requests

from .config import CREDS, DRY_RUN, SQUARE_API, SQUARE_SANDBOX_API, SQUARE_VERSION, USE_SANDBOX

log = logging.getLogger("pod.square")


class SquareError(Exception):
    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []


class SquareClient:
    """
    Auth: a Personal Access Token is fine for a single-seller self-hosted bot
    and is far simpler than OAuth. Create it in the Square Developer Dashboard
    under your application -> OAuth -> "Personal Access Token" (Production).

    Required scopes for this system:
      ITEMS_WRITE   -- catalog upsert, delete, images
      ITEMS_READ    -- verify listings
      ORDERS_READ   -- test metrics, refund detection
      PAYMENTS_READ -- payment.updated / refund.created webhooks
      MERCHANT_PROFILE_READ -- location id discovery
      CUSTOMERS_WRITE -- optional, for repeat-buyer email capture
      INVENTORY_READ -- optional
    """

    def __init__(self, token: str | None = None, dry_run: bool | None = None):
        self.token = token or CREDS.square_token
        self.dry_run = DRY_RUN if dry_run is None else dry_run
        self.base = SQUARE_SANDBOX_API if USE_SANDBOX else SQUARE_API
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
                "User-Agent": "pod-autopilot/1.0",
            }
        )

    # -- transport ---------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
        max_retries: int = 3,
    ) -> Any:
        url = f"{self.base}{path}"
        for attempt in range(max_retries + 1):
            if self.dry_run and method.upper() in ("POST", "PUT", "DELETE"):
                log.info("[DRY_RUN] %s %s %s", method, url, str(body)[:400])
                if path == "/v2/catalog/search":
                    # Simulate the Printful push having landed: echo the query
                    # back as a catalog item with purchasable variations.
                    kw = ((body or {}).get("text_query", {}).get("keywords") or ["DRY"])[0]
                    return {"objects": [
                        {"type": "ITEM", "id": "DRY_ITEM_1",
                         "item_data": {"name": kw + "t",
                                       "variations": [
                                           {"id": f"DRY_VAR_{i}", "type": "ITEM_VARIATION"}
                                           for i in range(5)]}}
                    ]}
                if path == "/v2/catalog/batch-retrieve":
                    ids = (body or {}).get("object_ids", [])
                    return {"objects": [
                        {"type": "ITEM_VARIATION", "id": i} for i in ids
                    ]}
                if path == "/v2/catalog/images":
                    return {"id_mappings": [{"client_supplied_id": "#img",
                                             "object_id": "DRY_IMG_1"}]}
                return {"dry_run": True, "id_mapping": {}}

            try:
                resp = self.session.request(
                    method, url, json=body, params=params, timeout=60
                )
            except requests.RequestException as exc:
                if attempt == max_retries:
                    raise SquareError(f"Network error on {url}: {exc}") from exc
                time.sleep(2**attempt)
                continue

            # Square returns 429 when another catalog write holds the per-seller
            # lock. Only ONE catalog update is processed at a time per seller
            # account -- so never issue parallel catalog writes.
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("Square 429 (catalog lock) -- waiting %.1fs", wait)
                time.sleep(wait + 0.5)
                continue

            if resp.status_code >= 500 and attempt < max_retries:
                time.sleep(2**attempt)
                continue

            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {"raw": resp.text[:500]}
                errs = payload.get("errors", [])
                raise SquareError(
                    f"{resp.status_code} {method} {path}: "
                    + "; ".join(e.get("detail", str(e)) for e in errs),
                    errs,
                )

            return resp.json() if resp.text else {}

        raise SquareError(f"Exhausted retries on {method} {url}")

    # -- catalog -----------------------------------------------------------

    def list_locations(self) -> list[dict]:
        return self._call("GET", "/v2/locations").get("locations", [])

    def search_catalog_items(
        self, text_query: str | None = None, limit: int = 100
    ) -> list[dict]:
        body: dict[str, Any] = {"limit": limit}
        if text_query:
            body["text_query"] = {"keywords": [text_query]}
        return self._call("POST", "/v2/catalog/search", body).get("objects", [])

    def retrieve_objects(self, object_ids: list[str]) -> list[dict]:
        if not object_ids:
            return []
        return self._call(
            "POST", "/v2/catalog/batch-retrieve", {"object_ids": object_ids}
        ).get("objects", [])

    def upsert_pod_item(
        self,
        *,
        name: str,
        description: str,
        variations: list[dict],
        category_id: str | None = None,
        image_object_id: str | None = None,
        reportable_id: str | None = None,
    ) -> dict:
        """
        Create/update one catalog ITEM with its ITEM_VARIATIONs in a single
        atomic batch. Square assigns real ids and returns an id_mapping from
        your #-prefixed temp ids.

        variations: [{"temp_id": "#v_m", "name": "M", "sku": "EXP-1-M",
                      "price_cents": 2499, "option_values": [...]}]

        IMPORTANT: pass reportable_id (the experiment id) into the SKU or a
        custom attribute. It is the only durable link back from a Square order
        line item to your experiment row. Without it you cannot attribute a
        sale, and an unattributable sale makes the whole test worthless.
        """
        item_temp = f"#item_{uuid.uuid4().hex[:8]}"
        objects: list[dict] = []

        var_objects = []
        for v in variations:
            vdata: dict[str, Any] = {
                "item_id": item_temp,
                "name": v.get("name"),
                "sku": v.get("sku"),
                "pricing_type": "FIXED_PRICING",
                "price_money": {
                    "amount": int(v["price_cents"]),
                    "currency": v.get("currency", "USD"),
                },
                # POD items are made to order. Do NOT track inventory -- Square
                # will zero it out and hide the listing after the first sale.
                "track_inventory": False,
                "stockable": True,
            }
            if v.get("option_values"):
                vdata["item_option_values"] = v["option_values"]
            if v.get("upc"):
                vdata["upc"] = v["upc"]
            var_objects.append(
                {
                    "type": "ITEM_VARIATION",
                    "id": v["temp_id"],
                    "present_at_all_locations": True,
                    "item_variation_data": vdata,
                }
            )

        item_data: dict[str, Any] = {
            "name": name,
            "description_html": description,
            "product_type": "REGULAR",
            "variations": var_objects,
            # PRIVATE keeps it out of the POS item grid but still sellable
            # online. For a POD store that also runs a physical POS, this stops
            # 200 shirt variants from polluting the register screen.
            "visibility": "PRIVATE",
            "ecom_visibility": "VISIBLE",   # accepted on write but READ-ONLY in
            "ecom_available": True,          # practice. See module docstring.
        }
        if category_id:
            item_data["category_id"] = category_id
        if image_object_id:
            item_data["image_id"] = image_object_id

        objects.append(
            {
                "type": "ITEM",
                "id": item_temp,
                "present_at_all_locations": True,
                "item_data": item_data,
            }
        )

        resp = self._call(
            "POST",
            "/v2/catalog/batch-upsert",
            {
                "idempotency_key": str(uuid.uuid4()),
                "batches": [{"objects": objects}],
            },
        )

        mapping = resp.get("id_mappings", [])
        real_item_id = None
        real_var_ids = []
        for m in mapping:
            if m.get("client_supplied_id") == item_temp:
                real_item_id = m.get("object_id")
            elif str(m.get("client_supplied_id", "")).startswith("#v_"):
                real_var_ids.append(m.get("object_id"))

        return {
            "item_id": real_item_id,
            "variation_ids": real_var_ids,
            "id_mappings": mapping,
            "objects": resp.get("objects", []),
            # The visibility flip is NOT done. Record that explicitly so the
            # Sentinel verifies against the live storefront.
            "visibility_confirmed": False,
        }

    def create_catalog_image(self, item_id: str, image_bytes: bytes, filename: str) -> str:
        """
        POST /v2/catalog/images -- multipart, not JSON. Needs the item id to
        exist first, so this is a second call after the upsert. Attach the
        returned image object to the item with another upsert that sets
        item_data.image_id.
        """
        if self.dry_run:
            log.info("[DRY_RUN] catalog image for %s (%d bytes)", item_id, len(image_bytes))
            return "DRY_IMAGE_ID"
        url = f"{self.base}/v2/catalog/images"
        files = {"image_file": (filename, image_bytes, "image/png")}
        data = {
            "request": json.dumps(
                {
                    "idempotency_key": str(uuid.uuid4()),
                    "object_id": item_id,
                    "image": {
                        "id": "#img",
                        "type": "IMAGE",
                        "image_data": {"caption": "Product mockup"},
                    },
                }
            )
        }
        # Separate session call: this endpoint is multipart/form-data.
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Square-Version": SQUARE_VERSION,
            },
            data=data,
            files=files,
            timeout=120,
        )
        if resp.status_code >= 400:
            raise SquareError(f"Image upload failed: {resp.status_code} {resp.text[:300]}")
        payload = resp.json()
        for m in payload.get("id_mappings", []):
            if m.get("client_supplied_id") == "#img":
                return m["object_id"]
        objs = payload.get("image", {}) or payload.get("images", [{}])
        return (objs[0] if isinstance(objs, list) else objs).get("id", "")

    def delete_object(self, object_id: str) -> dict:
        return self._call("DELETE", f"/v2/catalog/object/{object_id}")

    def upsert_category(self, name: str, temp_id: str = "#cat") -> str:
        resp = self._call(
            "POST",
            "/v2/catalog/batch-upsert",
            {
                "idempotency_key": str(uuid.uuid4()),
                "batches": [
                    {
                        "objects": [
                            {
                                "type": "CATEGORY",
                                "id": temp_id,
                                "present_at_all_locations": True,
                                "category_data": {"name": name},
                            }
                        ]
                    }
                ],
            },
        )
        for m in resp.get("id_mappings", []):
            if m.get("client_supplied_id") == temp_id:
                return m["object_id"]
        return ""

    # -- orders / metrics --------------------------------------------------

    def search_orders(
        self,
        location_ids: list[str] | None = None,
        created_after: str | None = None,
        states: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        query: dict[str, Any] = {"limit": limit, "return_entries": True}
        filt: dict[str, Any] = {}
        if location_ids:
            filt["location_ids"] = location_ids
        if created_after:
            filt["date_time_filter"] = {"created_at": {"start_at": created_after}}
        if states:
            filt["state_filter"] = {"states": states}
        if filt:
            query["filter"] = filt
        return self._call("POST", "/v2/orders/search", query).get("orders", [])

    def retrieve_order(self, order_id: str) -> dict:
        return self._call("POST", "/v2/orders/batch-retrieve", {"order_ids": [order_id]})[
            "orders"
        ][0]

    def sales_by_item(self, since_iso: str) -> dict[str, dict[str, float]]:
        """Aggregate line items across orders -> {item_variation_id: {units, revenue}}."""
        out: dict[str, dict[str, float]] = {}
        orders = self.search_orders(created_after=since_iso, states=["OPEN", "COMPLETED"])
        for o in orders:
            for line in o.get("line_items", []):
                vid = line.get("catalog_variation_id") or line.get("name", "?")
                qty = int(line.get("quantity", 0))
                gross = int(line.get("gross_sales_money", {}).get("amount", 0)) / 100.0
                bucket = out.setdefault(vid, {"units": 0.0, "revenue": 0.0})
                bucket["units"] += qty
                bucket["revenue"] += gross
        return out


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def verify_square_signature(
    raw_body: bytes, notification_url: str, signature_key: str, provided: str
) -> bool:
    """
    Square signs HMAC-SHA256 over (notification_url + raw_body), base64-encoded,
    sent in `x-square-hmacsha256-signature`.

    Two details people get wrong:
      * The notification URL is part of the signed content and must match
        byte-for-byte what is configured in the subscription.
      * You must verify the RAW body. JSON-parsing and re-serializing changes
        whitespace and breaks every signature.
    """
    if not signature_key or not provided:
        return False
    payload = notification_url.encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(signature_key.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, provided)


def create_webhook_subscription(
    token: str, notification_url: str, name: str = "pod-autopilot"
) -> dict:
    """
    One-time setup. Run with a Personal Access Token.

    Events this system needs:
      order.created / order.updated  -- attribute sales to experiments
      payment.updated                -- confirm money actually cleared
      refund.created / refund.updated -- refund-rate guardrail
      catalog.version_updated        -- detect external catalog edits
    """
    client = SquareClient(token=token)
    return client._call(
        "POST",
        "/v2/webhooks/subscriptions",
        {
            "idempotency_key": str(uuid.uuid4()),
            "subscription": {
                "name": name,
                "notification_url": notification_url,
                "api_version": SQUARE_VERSION,
                "enabled": True,
                "event_types": [
                    "order.created",
                    "order.updated",
                    "payment.created",
                    "payment.updated",
                    "refund.created",
                    "refund.updated",
                    "catalog.version_updated",
                ],
            },
        },
    )
