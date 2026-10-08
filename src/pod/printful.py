"""
Printful API client with real rate-limit handling.

Two limits matter:
  * General API: 120 req/min, leaky bucket, surfaced via X-Ratelimit-* headers.
  * Mockup task creation: 2 req/min on a NEW store, 10 req/min once the store
    has >= $10 of fulfilled orders. Plus 20,000 generated files/day.

A 429 costs a 60-second lockout of the whole endpoint, which in an unattended
pipeline means a cascade. So: local token bucket, honor Retry-After, and read
the live headers rather than trusting the configured ceiling.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from .config import (
    CREDS,
    DRY_RUN,
    PRINTFUL_API,
    PRINTFUL_API_V2,
    PRINTFUL_DAILY_MOCKUP_FILES,
    PRINTFUL_RPM_GENERAL,
    PRINTFUL_RPM_MOCKUP,
    RATE_LIMIT_SAFETY,
)

log = logging.getLogger("pod.printful")


class TokenBucket:
    """Thread-safe leaky bucket. Printful's own limiter is a leaky bucket, so
    matching its shape keeps the two in phase."""

    def __init__(self, rate_per_min: float, safety: float = RATE_LIMIT_SAFETY):
        self.capacity = max(1.0, rate_per_min * safety)
        self.refill_per_sec = self.capacity / 60.0
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0, timeout: float = 600.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec
                )
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return True
                wait = (n - self.tokens) / self.refill_per_sec
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 5.0))


class PrintfulError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class PrintfulClient:
    def __init__(self, token: str | None = None, dry_run: bool | None = None):
        self.token = token or CREDS.printful_token
        self.dry_run = DRY_RUN if dry_run is None else dry_run
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "pod-autopilot/1.0",
            }
        )
        self._general = TokenBucket(PRINTFUL_RPM_GENERAL)
        self._mockup = TokenBucket(PRINTFUL_RPM_MOCKUP)
        self._mockup_files_today = 0
        self._mockup_day = time.strftime("%Y-%m-%d")

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        base: str = PRINTFUL_API,
        json_body: dict | None = None,
        params: dict | None = None,
        bucket: TokenBucket | None = None,
        max_retries: int = 4,
    ) -> Any:
        url = f"{base}{path}"
        limiter = bucket or self._general

        for attempt in range(max_retries + 1):
            if self.dry_run and method.upper() == "GET":
                fixture = self._dry_fixture(path)
                if fixture is not None:
                    log.info("[DRY_RUN] GET %s -> fixture", path)
                    return fixture
                # Unfixtured reads fall through to the real API; a 401 here in
                # dry-run is informative, not fatal.

            if not limiter.acquire():
                raise PrintfulError(f"Rate limiter timeout for {method} {path}")

            if self.dry_run and method.upper() in ("POST", "PUT", "DELETE"):
                log.info("[DRY_RUN] %s %s %s", method, url, _truncate(json_body))
                return self._dry_write_fixture(method, path, json_body)

            try:
                resp = self.session.request(
                    method, url, json=json_body, params=params, timeout=90
                )
            except requests.RequestException as exc:
                if attempt == max_retries:
                    raise PrintfulError(f"Network error on {url}: {exc}") from exc
                time.sleep(2**attempt)
                continue

            self._absorb_headers(resp)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "60") or 60)
                log.warning(
                    "Printful 429 on %s -- locked out, sleeping %ss (attempt %d)",
                    path, retry_after, attempt + 1,
                )
                time.sleep(retry_after + 1)
                continue

            if resp.status_code >= 500:
                if attempt == max_retries:
                    raise PrintfulError(f"{resp.status_code} from {url}", resp.status_code)
                time.sleep(2**attempt)
                continue

            if resp.status_code >= 400:
                raise PrintfulError(
                    f"{resp.status_code} from {url}: {resp.text[:500]}",
                    resp.status_code,
                    _safe_json(resp),
                )

            return _safe_json(resp)

        raise PrintfulError(f"Exhausted retries on {method} {url}")

    def _dry_write_fixture(self, method: str, path: str, body: dict | None) -> dict:
        """Shape-correct responses for dry-run writes, so the pipeline can run
        its full chain (mockup task -> poll -> sync product) with no token."""
        import re
        import time as _t
        if re.fullmatch(r"/mockup-generator/create-task/\d+", path):
            key = f"DRYTASK{int(_t.time())}"
            self._dry_mockup_tasks = getattr(self, "_dry_mockup_tasks", {})
            self._dry_mockup_tasks[key] = body or {}
            return {"code": 200, "result": {"task_key": key}}
        if path == "/store/products" and method == "POST":
            sid = 900000 + (getattr(self, "_dry_sync_n", 0))
            self._dry_sync_n = sid - 900000 + 1
            return {"code": 200, "result": {
                "sync_product": {"id": sid, "name": (body or {}).get("sync_product", {}).get("name")},
                "sync_variants": [
                    {"id": 100000 + i, "variant_id": v.get("variant_id"),
                     "sku": v.get("sku"), "retail_price": v.get("retail_price")}
                    for i, v in enumerate((body or {}).get("sync_variants", []))
                ],
            }}
        if path.startswith("/store/products/") and method == "DELETE":
            return {"code": 200, "result": {"deleted": True}}
        return {"code": 200, "result": {"dry_run": True, "id": 0}}

    def _absorb_headers(self, resp: requests.Response) -> None:
        """Printful tells you your real limits. Believe it over the config."""
        remaining = resp.headers.get("X-Ratelimit-Remaining")
        reset = resp.headers.get("X-Ratelimit-Reset")
        if remaining is not None:
            try:
                rem = int(remaining)
                if rem <= 2 and reset:
                    # Nearly empty bucket: stall locally instead of eating a 429.
                    time.sleep(min(float(reset) + 0.5, 60.0))
            except ValueError:
                pass

    def _mockup_budget(self, files: int) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._mockup_day:
            self._mockup_day = today
            self._mockup_files_today = 0
        if self._mockup_files_today + files > PRINTFUL_DAILY_MOCKUP_FILES:
            raise PrintfulError(
                f"Daily mockup file budget exhausted "
                f"({self._mockup_files_today}/{PRINTFUL_DAILY_MOCKUP_FILES})"
            )
        self._mockup_files_today += files

    # -- catalog -----------------------------------------------------------

    def _dry_fixture(self, path: str) -> dict | None:
        """
        DRY_RUN read fixtures. Catalog reads still need a real token in live
        mode; in dry-run we synthesize the shapes the pipeline depends on so
        the whole chain can be exercised offline with no credentials.

        These are SHAPES, not promises: the variant ids and printfile
        dimensions mirror Printful's published docs for the BC3001, but your
        first real run must confirm them against GET /products/71.
        """
        import re
        m = re.fullmatch(r"/mockup-generator/printfiles/(\d+)", path)
        if m:
            return {"code": 200, "result": {
                "product_id": int(m.group(1)),
                "available_placements": {"front": "Front print", "back": "Back print"},
                "printfiles": [{"printfile_id": 1, "width": 4500, "height": 5400,
                                "dpi": 300, "fill_mode": "fit", "can_rotate": False}],
                "variant_printfiles": [{"variant_id": v, "placements": {"front": 1}}
                                       for v in (4012, 4013, 4014, 4017, 4018)],
                "option_groups": ["Men's", "Women's"], "options": ["Front", "Back"],
            }}
        m = re.fullmatch(r"/products/variant/(\d+)", path)
        if m:
            return {"code": 200, "result": {
                "id": int(m.group(1)), "price": 13.25, "retail_price": 13.25,
                "sku": f"DRY-{m.group(1)}", "name": "DRY RUN variant",
            }}
        m = re.fullmatch(r"/products/(\d+)", path)
        if m:
            return {"code": 200, "result": {
                "id": int(m.group(1)), "name": "DRY RUN product",
                "variants": [{"id": v} for v in (4012, 4013, 4014, 4017, 4018)],
            }}
        if path == "/mockup-generator/task":
            return {"code": 200, "result": {
                "status": "completed",
                "mockups": [
                    {"mockup_url": f"https://assets.example.test/drymockup_{i}.png",
                     "variant_id": v, "placement": "front"}
                    for i, v in enumerate((4012, 4013, 4014, 4017, 4018))
                ],
            }}
        if path == "/stores":
            return {"code": 200, "result": [{"id": 0, "name": "DRY RUN store"}]}
        if path == "/orders":
            return {"code": 200, "result": []}
        return None

    def list_catalog_products(self, limit: int = 100) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            data = self._request(
                "GET", "/products", params={"limit": limit, "offset": offset}
            )
            batch = data.get("result", [])
            out.extend(batch)
            if len(batch) < limit:
                return out
            offset += limit

    def get_product(self, product_id: int) -> dict:
        return self._request("GET", f"/products/{product_id}")["result"]

    def get_variant(self, variant_id: int) -> dict:
        return self._request("GET", f"/products/variant/{variant_id}")["result"]

    def get_printfiles(self, product_id: int, technique: str = "dtg") -> dict:
        """Print-area dimensions and DPI for a product. Always call this rather
        than assuming -- the same tee differs between DTG and embroidery."""
        return self._request(
            "GET",
            f"/mockup-generator/printfiles/{product_id}",
            params={"technique": technique},
        )["result"]

    # -- mockups -----------------------------------------------------------

    def create_mockup_task(
        self,
        product_id: int,
        variant_ids: list[int],
        image_url: str,
        placement: str = "front",
        fmt: str = "png",
        option_groups: list[str] | None = None,
        position: dict | None = None,
    ) -> str:
        """Returns a task_key. Poll get_mockup_task() until completed."""
        self._mockup_budget(len(variant_ids))
        file_entry: dict[str, Any] = {"type": placement, "image_url": image_url}
        if position:
            file_entry["position"] = position
        body = {
            "variant_ids": variant_ids,
            "format": fmt,
            "files": [file_entry],
        }
        if option_groups:
            body["option_groups"] = option_groups
        data = self._request(
            "POST",
            f"/mockup-generator/create-task/{product_id}",
            json_body=body,
            bucket=self._mockup,
        )
        return data["result"]["task_key"]

    def get_mockup_task(self, task_key: str) -> dict:
        return self._request(
            "GET", "/mockup-generator/task", params={"task_key": task_key}
        )["result"]

    def wait_for_mockups(
        self, task_key: str, timeout: float = 600.0, poll: float = 5.0
    ) -> dict:
        """Mockup generation is async and typically lands in 10-60s."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.get_mockup_task(task_key)
            status = task.get("status")
            if status == "completed":
                return task
            if status == "failed":
                raise PrintfulError(f"Mockup task failed: {task}")
            time.sleep(poll)
        raise PrintfulError(f"Mockup task {task_key} timed out after {timeout}s")

    def mockup_urls(self, task: dict) -> list[str]:
        """One file per variant x style x placement combination."""
        seen: list[str] = []
        for entry in task.get("mockups", []):
            url = entry.get("mockup_url")
            if url and url not in seen:
                seen.append(url)
        return seen

    # -- sync products (this is what pushes to Square) ---------------------

    def create_sync_product(
        self,
        name: str,
        variants: list[dict],
        thumbnail_url: str | None = None,
        description: str | None = None,
    ) -> dict:
        """
        variants entries look like:
          {"variant_id": 4012, "retail_price": "24.99",
           "files": [{"type": "front", "url": "https://.../design.png"}],
           "sku": "MY-SKU-4012"}

        If your Printful store is connected to Square, creating a sync product
        here is what publishes the listing to Square. That is the entire
        integration -- there is no separate Square-side product push.
        """
        sync_product: dict[str, Any] = {"name": name}
        if thumbnail_url:
            sync_product["thumbnail"] = thumbnail_url
        if description:
            sync_product["description"] = description
        body = {"sync_product": sync_product, "sync_variants": variants}
        data = self._request("POST", "/store/products", json_body=body)
        return data["result"]

    def get_sync_product(self, sync_id: int) -> dict:
        return self._request("GET", f"/store/products/{sync_id}")["result"]

    def list_sync_products(self, limit: int = 100) -> list[dict]:
        return self._request("GET", "/store/products", params={"limit": limit}).get(
            "result", []
        )

    def update_sync_product(self, sync_id: int, sync_product: dict) -> dict:
        return self._request(
            "PUT", f"/store/products/{sync_id}", json_body={"sync_product": sync_product}
        )["result"]

    def delete_sync_product(self, sync_id: int) -> dict:
        """The kill switch at the fulfillment layer. Deleting the sync product
        removes the Square listing and prevents further fulfillment."""
        return self._request("DELETE", f"/store/products/{sync_id}")["result"]

    def list_stores(self) -> list[dict]:
        return self._request("GET", "/stores").get("result", [])

    # -- orders ------------------------------------------------------------

    def list_orders(self, status: str | None = None, limit: int = 50) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return self._request("GET", "/orders", params=params).get("result", [])

    def get_order(self, order_id: int) -> dict:
        return self._request("GET", f"/orders/{order_id}")["result"]

    def needs_approval_orders(self) -> list[dict]:
        """
        Orders sitting in NEEDS_APPROVAL are the #1 silent failure in
        Printful<->store automation. They imported but did not submit, so the
        customer paid and nothing is being printed. Usually caused by a product
        that was not synced at order time, or 'Manually confirm imported
        orders' being on. The Sentinel polls this and alarms.
        """
        return self.list_orders(status="NEEDS_APPROVAL")


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:1000]}


def _truncate(obj: Any, n: int = 400) -> str:
    s = str(obj)
    return s if len(s) <= n else s[:n] + "..."


# ---------------------------------------------------------------------------
# Reference catalog ids. Verify with list_catalog_products() before trusting --
# Printful adds and retires products.
# ---------------------------------------------------------------------------
KNOWN_PRODUCTS = {
    71: "Bella+Canvas 3001 Unisex Jersey Tee (the POD workhorse)",
    162: "Gildan 64000 Unisex Softstyle Tee",
    191: "Gildan 18500 Unisex Heavy Blend Hoodie",
    19: "11oz Ceramic Mug",
    168: "All-Over Print Unisex T-Shirt",
}
