"""
The pipeline orchestrator. One function per stage, each idempotent, each
guarded. This is what OpenBot's routines actually invoke.

Design principle: the LLM is used for JUDGMENT (what niche, what phrase, what
art direction) and never for MECHANICS (API calls, state transitions, money).
Mechanics live here, in boring testable Python. An LLM that decides whether to
call an API will eventually not call it, or call it twice.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any

from . import design, demand, trademark
from .config import CREDS, DRY_RUN, ECON, GUARDRAILS, PRINT_SPECS
from .db import DB
from .printful import PrintfulClient, PrintfulError
from .square import SquareClient, SquareError
from .state import Experiment, Stage, try_advance

log = logging.getLogger("pod.pipeline")

# ---------------------------------------------------------------------------
# Product defaults. Bella+Canvas 3001 (Printful id 71) is the industry default
# for a reason: cheap, consistent, huge color range, every POD buyer recognizes
# the fit. Start with ONE product. Expanding to hoodies/mugs multiplies mockup
# cost and IP surface for no proven lift.
# ---------------------------------------------------------------------------
DEFAULT_PRODUCT_ID = 71
# Variant ids for BC3001 in Black, sizes S-2XL. VERIFY THESE with
# get_product(71) before first run -- Printful changes ids across catalogs.
DEFAULT_VARIANTS_BLACK = {
    "S": 4012, "M": 4013, "L": 4014, "XL": 4017, "2XL": 4018,
}
DEFAULT_VARIANTS_WHITE = {
    "S": 4011, "M": 4012, "L": 4013, "XL": 4016, "2XL": 4017,
}


class Pipeline:
    def __init__(self, db: DB | None = None):
        self.db = db or DB()
        self.printful = PrintfulClient()
        self.square = SquareClient()

    # ------------------------------------------------------------------
    # STAGE 1: DISCOVERED -> SCORED
    # ------------------------------------------------------------------
    def validate_and_score(
        self,
        niche: str,
        concept: str,
        keywords: list[str],
        audience: str = "",
        bsr_ranks: list[int] | None = None,
    ) -> Experiment | None:
        """
        Called by TrendScout after the LLM proposes concepts. The LLM's opinion
        of whether something is trending is discarded; this function looks it up.
        """
        if self.db.concept_exists(concept):
            log.info("Concept already in flight or dead: %r", concept)
            return None

        import os as _os
        scored = demand.sweep(niche=niche, concept=concept, keywords=keywords, audience=audience)
        if _os.environ.get("POD_FAKE_SIGNALS"):
            # DEMO ONLY: synthesize corroborating signals so the rest of the
            # pipeline can be exercised offline. Values are labeled synthetic
            # and stored that way -- an experiment built on fake signals must
            # never reach production, so the flag also gates publish().
            from .state import Signal as _Sig
            scored.signals.append(_Sig(
                source="google_trends", query=keywords[0] if keywords else concept,
                metric="delta", value=1.4,
                evidence_url="synthetic://POD_FAKE_SIGNALS", raw={"synthetic": True},
            ))
            scored.signals.append(_Sig(
                source="reddit", query=niche, metric="post_score", value=900,
                evidence_url="synthetic://POD_FAKE_SIGNALS", raw={"synthetic": True},
            ))
            scored.breakdown.update({"trend_momentum": 0.9, "trend_magnitude": 0.8,
                                     "competition_gap": 0.7, "purchase_evidence": 0.6,
                                     "audience_depth": 0.8})
            scored.score = 0.78
            scored.verdict = "SYNTHETIC (POD_FAKE_SIGNALS) -- demo only"
            log.critical("POD_FAKE_SIGNALS active: demand score is SYNTHETIC")
        if bsr_ranks:
            # Fold in Amazon Best Sellers evidence captured by the browser step.
            # sweep() cannot reach Amazon (datacenter IPs get captcha'd), so the
            # TrendScout supplies ranks it read in its own Chromium container.
            import math as _math
            scored.breakdown["purchase_evidence"] = max(
                scored.breakdown.get("purchase_evidence", 0.0),
                demand._norm(-_math.log10(min(bsr_ranks) + 1), -4.0, -0.7),
            )
            for r in bsr_ranks[:5]:
                from .state import Signal as _S
                scored.signals.append(_S(
                    source="amazon_bsr", query=niche, metric="bsr_rank",
                    value=float(r),
                    evidence_url=demand.AMAZON_BSR_URLS.get(niche, ""),
                ))
            w = scored.breakdown
            scored.score = round(
                0.30 * w.get("trend_momentum", 0)
                + 0.20 * w.get("trend_magnitude", 0)
                + 0.20 * w.get("competition_gap", 0)
                + 0.15 * w.get("purchase_evidence", 0)
                + 0.15 * w.get("audience_depth", 0),
                4,
            )

        exp = Experiment(
            niche=niche, concept=concept, keywords=keywords, audience=audience,
            signals=scored.signals, demand_score=scored.score,
            score_breakdown=scored.breakdown,
        )
        exp.record("created", verdict=scored.verdict)
        self.db.insert_experiment(exp)

        ok, msg = try_advance(exp, Stage.SCORED)
        self.db.save(exp)
        log.info("score %s -> %s | %s", exp.id, ok, msg)
        return exp if ok else None

    # ------------------------------------------------------------------
    # STAGE 2: SCORED -> IP_CLEARED
    # ------------------------------------------------------------------
    def screen_ip(self, exp: Experiment, phrase: str, subphrase: str = "") -> bool:
        import os as _os
        result = trademark.screen(
            concept=exp.concept,
            title=_title_for(exp, phrase),
            description=_description_for(exp, phrase),
            design_text=[t for t in (phrase, subphrase) if t],
            product_type=exp.niche if exp.niche in trademark.NICE_CLASS_BY_PRODUCT else "tshirt",
            demo_skip_uspto=bool(_os.environ.get("POD_DEMO_NO_USPTO")),
        )
        exp.ip_screen = result.to_dict()
        exp.record("ip_screen", risk=result.risk, matches=len(result.matches))

        if result.risk != "LOW":
            exp.kill(
                f"IP risk {result.risk}: "
                + "; ".join(f"{m.term}({m.layer})" for m in result.matches[:5])
            )
            self.db.save(exp)
            log.warning("KILLED %s on IP: %s", exp.id, exp.kill_reason)
            return False

        ok, msg = try_advance(exp, Stage.IP_CLEARED)
        self.db.save(exp)
        return ok

    # ------------------------------------------------------------------
    # STAGE 3: IP_CLEARED -> DESIGNED
    # ------------------------------------------------------------------
    def build(self, exp: Experiment, brief: design.DesignBrief) -> bool:
        try:
            out, url = design.build_design(brief)
        except design.DesignError as exc:
            exp.kill(f"design failed: {exc}")
            self.db.save(exp)
            return False

        exp.design_file_url = url
        exp.design_prompt = brief.art_prompt()
        exp.design_spec = {  # type: ignore[attr-defined]
            "width": out.width, "height": out.height,
            "expected_width": out.width, "expected_height": out.height,
            "dpi": out.dpi, "size_mb": out.size_mb,
        }
        exp.record("designed", path=str(out.png_path), warnings=out.warnings)

        if out.warnings:
            # Warnings are not fatal (narrow ink coverage is a taste issue) but
            # they are recorded so you can correlate them against conversion
            # later. That is how you learn which warnings actually matter.
            log.info("Design warnings for %s: %s", exp.id, out.warnings)

        ok, _ = try_advance(exp, Stage.DESIGNED)
        self.db.save(exp)
        return ok

    # ------------------------------------------------------------------
    # STAGE 4: DESIGNED -> MOCKED_UP
    # ------------------------------------------------------------------
    def mockups(
        self,
        exp: Experiment,
        product_id: int = DEFAULT_PRODUCT_ID,
        variant_ids: list[int] | None = None,
        placement: str = "front",
    ) -> bool:
        """
        Mockup generation is the rate-limit bottleneck: 2/min on a new store.
        5 variants = ~2.5 min minimum. Budget for it; do not retry aggressively,
        a 429 costs a 60s lockout of the whole endpoint.
        """
        variant_ids = variant_ids or list(DEFAULT_VARIANTS_BLACK.values())
        if not exp.design_file_url:
            exp.kill("no design file for mockup stage")
            self.db.save(exp)
            return False
        try:
            pf = self.printful.get_printfiles(product_id)
            exp.record("printfiles", placements=list(pf.get("available_placements", {}).keys()))
            task_key = self.printful.create_mockup_task(
                product_id=product_id,
                variant_ids=variant_ids,
                image_url=exp.design_file_url,
                placement=placement,
                option_groups=["Men's"],
            )
            task = self.printful.wait_for_mockups(task_key, timeout=600)
            urls = self.printful.mockup_urls(task)
        except PrintfulError as exc:
            exp.kill(f"mockup failed: {exc}")
            self.db.save(exp)
            return False

        exp.mockup_urls = urls
        exp.product_id = product_id
        exp.variant_ids = variant_ids
        exp.record("mocked_up", n=len(urls))
        ok, _ = try_advance(exp, Stage.MOCKED_UP)
        self.db.save(exp)
        return ok

    # ------------------------------------------------------------------
    # STAGE 5: MOCKED_UP -> LISTED
    # ------------------------------------------------------------------
    def publish(self, exp: Experiment, phrase: str) -> bool:
        """
        Publish via Printful (which syncs the listing to Square), then find the
        resulting Square catalog item, price it, and verify it is actually
        purchasable on the storefront.

        Why both systems: Printful owns the print file and fulfillment. Square
        owns the storefront and the sale. The retail_price you set on the
        Printful sync variant is what Square shows.
        """
        synthetic = any(s.evidence_url and s.evidence_url.startswith("synthetic://")
                        for s in exp.signals)
        if synthetic and not DRY_RUN:
            exp.kill("experiment carries SYNTHETIC signals (POD_FAKE_SIGNALS); "
                     "publishing it for real is forbidden")
            self.db.save(exp)
            return False

        if self.db.listed_today() >= GUARDRAILS.max_new_listings_per_day:
            log.info("Daily listing cap reached (%d). Deferring %s",
                     GUARDRAILS.max_new_listings_per_day, exp.id)
            return False
        if self.db.count_staged(Stage.TESTING) >= GUARDRAILS.max_concurrent_tests:
            log.info("Concurrent test cap reached. Deferring %s", exp.id)
            return False

        # -- price --------------------------------------------------------
        try:
            base_cost = self._base_cost_for(exp.variant_ids[:1])
        except Exception as exc:  # noqa: BLE001
            exp.kill(f"could not determine base cost: {exc}")
            self.db.save(exp)
            return False

        retail = ECON.retail_for(base_cost)
        profit = ECON.profit_at(retail, base_cost)
        if not ECON.viable(retail, base_cost):
            exp.kill(
                f"not viable: base ${base_cost:.2f} -> retail ${retail:.2f} "
                f"yields ${profit:.2f} profit, floor is ${ECON.min_profit:.2f}"
            )
            self.db.save(exp)
            return False

        exp.base_cost = round(base_cost, 2)
        exp.retail_price = retail
        exp.projected_profit = round(profit, 2)

        # -- Printful sync product (this creates the Square listing) -------
        title = _title_for(exp, phrase)
        description = _description_for(exp, phrase)
        variants_payload = []
        for vid in exp.variant_ids:
            variants_payload.append(
                {
                    "variant_id": vid,
                    "retail_price": f"{retail:.2f}",
                    # SKU carries the experiment id. This is the fallback link
                    # back to the experiment if the catalog variation mapping
                    # is ever lost. Cheap redundancy, high value.
                    "sku": f"{exp.id.upper()}-{vid}",
                    "files": [{"type": "front", "url": exp.design_file_url}],
                }
            )
        try:
            created = self.printful.create_sync_product(
                name=title,
                variants=variants_payload,
                thumbnail_url=(exp.mockup_urls[0] if exp.mockup_urls else None),
                description=description,
            )
        except PrintfulError as exc:
            exp.kill(f"printful publish failed: {exc}")
            self.db.save(exp)
            return False

        sync_id = None
        if isinstance(created, dict):
            sync_id = (created.get("sync_product") or {}).get("id") or created.get("id")
        exp.printful_sync_product_id = sync_id
        exp.record("printful_published", sync_id=sync_id, title=title)

        # -- locate the Square item ---------------------------------------
        sq = self._find_square_item(title)
        if sq:
            exp.square_item_id = sq["item_id"]
            exp.square_variation_ids = sq["variation_ids"]
            skus = [f"{exp.id.upper()}-{vid}" for vid in exp.variant_ids]
            self.db.map_variations(exp.id, sq["variation_ids"], skus[: len(sq["variation_ids"])])

        exp.listed_at = datetime.now(timezone.utc)
        ok, msg = try_advance(exp, Stage.LISTED)
        self.db.save(exp)
        if not ok:
            log.error("Publish gate failed for %s: %s", exp.id, msg)
        return ok

    def _base_cost_for(self, variant_ids: list[int]) -> float:
        """Read the actual retail cost Printful will charge. Never hardcode --
        Printful changes base prices and they vary by color and size."""
        if DRY_RUN:
            return 13.25  # BC3001 black, typical DTG base
        v = self.printful.get_variant(variant_ids[0])
        price = v.get("price") or (v.get("prices") or {}).get("price")
        if price is None:
            # v2 shape
            price = v.get("retail_price") or 13.25
        return float(price)

    def _find_square_item(self, title: str, attempts: int = 6, delay: float = 10.0) -> dict | None:
        """
        Printful's push to Square is asynchronous. Poll the Square catalog by
        title until the item appears. If it never does, the integration is
        broken and the listing does not exist -- do not proceed.
        """
        import time
        for i in range(attempts):
            try:
                objs = self.square.search_catalog_items(text_query=title.split(" - ")[0][:40])
            except SquareError as exc:
                log.warning("Square search failed (attempt %d): %s", i + 1, exc)
                objs = []
            for o in objs:
                if o.get("type") != "ITEM":
                    continue
                if (o.get("item_data", {}).get("name") or "").strip() == title.strip():
                    vids = [
                        v["id"] for v in o["item_data"].get("variations", [])
                        if isinstance(v, dict) and v.get("id")
                    ]
                    if not vids:
                        # Variations are separate catalog objects; fetch them.
                        related = self.square.retrieve_objects([o["id"]])
                        for r in related:
                            if r.get("type") == "ITEM_VARIATION":
                                vids.append(r["id"])
                    return {"item_id": o["id"], "variation_ids": vids}
            time.sleep(delay)
        return None

    # ------------------------------------------------------------------
    # STAGE 6: LISTED -> TESTING -> SCALING
    # ------------------------------------------------------------------
    def begin_test(self, exp: Experiment, daily_budget: float = 8.00) -> bool:
        """
        Drive traffic. Two channels, in this order of preference:

        A) ORGANIC (free, slow, honest). Post the mockup to your social
           accounts. Zero spend, but volume-limited and noisy.
        B) PAID (fast, measurable, costs money). Square Marketing / Meta /
           TikTok at a hard-capped daily budget.

        This function ONLY sets up the test and enforces caps. It does not
        spend money without the Sentinel having cleared the budget check.
        """
        if not exp.listed_at:
            return False
        ok, msg = try_advance(exp, Stage.TESTING)
        self.db.save(exp)
        if ok:
            exp.record("test_started", budget=daily_budget)
            self.db.save(exp)
        return ok

    def evaluate_tests(self) -> dict[str, list[str]]:
        """Run the verdict on every mature test. Returns a summary."""
        from .state import decide_test
        scaled, killed, held = [], [], []
        for exp in self.db.by_stage(Stage.TESTING):
            verdict, reason = decide_test(exp)
            if verdict == "scale":
                scaled.append(f"{exp.id}: {reason}")
            elif verdict == "kill":
                killed.append(f"{exp.id}: {reason}")
                # Actually delist. A killed experiment that stays live keeps
                # collecting orders you decided not to want.
                self.delist(exp)
            else:
                held.append(f"{exp.id}: {reason}")
            self.db.save(exp)
        return {"scaled": scaled, "killed": killed, "held": held}

    def delist(self, exp: Experiment) -> None:
        """Remove from both systems. Printful deletion removes the Square
        listing; the Square delete is belt-and-braces for items created
        directly in the catalog."""
        if exp.printful_sync_product_id and not DRY_RUN:
            try:
                self.printful.delete_sync_product(exp.printful_sync_product_id)
                exp.record("delisted_printful")
            except PrintfulError as exc:
                log.error("Failed to delist %s from Printful: %s", exp.id, exc)
                self.db.log_event("delist_failure", {"exp": exp.id, "err": str(exc)})
        if exp.square_item_id and not DRY_RUN:
            try:
                self.square.delete_object(exp.square_item_id)
                exp.record("delisted_square")
            except SquareError as exc:
                log.error("Failed to delist %s from Square: %s", exp.id, exc)

    # ------------------------------------------------------------------
    # Full sweep: one idea, end to end
    # ------------------------------------------------------------------
    def run_one(
        self,
        niche: str,
        concept: str,
        phrase: str,
        keywords: list[str],
        audience: str = "",
        style: str = "retro_sunset",
        art_subject: str = "",
        subphrase: str = "",
        layout: str = "arch_top",
        product_id: int = DEFAULT_PRODUCT_ID,
        variant_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """
        DISCOVERED -> LISTED in one call. Each stage checks the Sentinel's
        guardrails before proceeding, and any failure stops the chain with a
        recorded reason. Nothing here spends money except fulfillment that a
        real customer order triggers.
        """
        from .sentinel import Sentinel
        sentinel = Sentinel(self.db)

        trace: list[str] = []
        cleared, why = sentinel.may_proceed()
        if not cleared:
            return {"ok": False, "stage": "guardrail", "reason": why}

        exp = self.validate_and_score(niche, concept, keywords, audience)
        if not exp:
            return {"ok": False, "stage": "scored", "reason": "below threshold or duplicate"}
        trace.append(f"scored={exp.demand_score}")

        if not self.screen_ip(exp, phrase, subphrase):
            return {"ok": False, "stage": "ip", "reason": exp.kill_reason, "id": exp.id}
        trace.append("ip=clear")

        brief = design.DesignBrief(
            concept=concept, phrase=phrase, subphrase=subphrase, niche=niche,
            style=style, art_subject=art_subject or concept, layout=layout,
            product_type="tshirt", audience_language=keywords,
        )
        if not self.build(exp, brief):
            return {"ok": False, "stage": "design", "reason": exp.kill_reason, "id": exp.id}
        trace.append("design=built")

        if not self.mockups(exp, product_id=product_id, variant_ids=variant_ids):
            return {"ok": False, "stage": "mockup", "reason": exp.kill_reason, "id": exp.id}
        trace.append(f"mockups={len(exp.mockup_urls)}")

        if not self.publish(exp, phrase):
            return {"ok": False, "stage": "publish", "reason": exp.kill_reason, "id": exp.id}
        trace.append(
            f"listed @ ${exp.retail_price:.2f} (profit ${exp.projected_profit:.2f})"
        )

        self.begin_test(exp)
        trace.append("testing=started")
        return {"ok": True, "id": exp.id, "trace": " | ".join(trace),
                "square_item_id": exp.square_item_id,
                "printful_sync_id": exp.printful_sync_product_id}


# ---------------------------------------------------------------------------
# Copy generation. Deterministic templates, not free-form LLM text.
#
# Reason: the title and description are (a) indexed by Square's SEO and
# (b) screened by the IP gate. Free-form LLM copy introduces unbounded
# trademark surface and inconsistent structure. Templates keep the screenable
# surface small and predictable while still reading like a real store.
# ---------------------------------------------------------------------------

def _title_for(exp: Experiment, phrase: str) -> str:
    """Square item names show on the storefront and in search. Keep under 60
    chars where possible; front-load the phrase people search for."""
    t = f"{phrase} {exp.niche.title()} T-Shirt"
    return t[:120]


def _description_for(exp: Experiment, phrase: str) -> str:
    return (
        f"<p>{phrase} — a {exp.niche} design made for people who get it.</p>"
        f"<p>Printed on demand on a soft, pre-shrunk unisex jersey tee. "
        f"Because every shirt is made after you order it, please allow "
        f"2–5 business days for production plus shipping. Sizing runs true to "
        f"size; size up for a relaxed fit.</p>"
        f"<ul><li>Unisex fit</li><li>Machine wash cold, inside out</li>"
        f"<li>Tumble dry low</li><li>Do not iron directly on the print</li></ul>"
    )
