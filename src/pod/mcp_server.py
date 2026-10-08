"""
MCP server exposing the pipeline to OpenBot Bots.

This is the seam between the LLM and the mechanics. Bots are granted individual
tools from here in the tenant YAML (`grants.mcp_tools`). A Bot can only call
what it is granted, and every call passes through OpenBot's policy gateway and
lands in the audit trail.

Design rule enforced by this file's shape: EVERY tool here is deterministic
Python. The LLM chooses WHICH tool to call and WITH WHAT ARGUMENTS. It never
decides whether a gate passes, what the price is, or whether money moves. Those
answers come from the state machine, the Sentinel, and the economics function.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import design, demand, trademark
from .config import DRY_RUN, ECON, GUARDRAILS
from .db import DB
from .pipeline import DEFAULT_PRODUCT_ID, DEFAULT_VARIANTS_BLACK, Pipeline
from .sentinel import Sentinel
from .state import Experiment, Stage

log = logging.getLogger("pod.mcp")

mcp = FastMCP("pod-pipeline")
db = DB()
pipe = Pipeline(db)
sentinel = Sentinel(db)


def _guard() -> dict[str, Any] | None:
    """Every mutating tool checks the Sentinel first. One place, so it cannot
    be forgotten by a new tool."""
    ok, why = sentinel.may_proceed()
    if not ok:
        return {"ok": False, "blocked_by_sentinel": why}
    return None


# ---------------------------------------------------------------------------
# Trend Scout
# ---------------------------------------------------------------------------

@mcp.tool()
def sweep_demand(
    niche: str, concept: str, keywords: list[str], audience: str = ""
) -> dict[str, Any]:
    """
    Validate a candidate concept against live data. Returns the demand score,
    the per-dimension breakdown, the corroborating sources, and evidence URLs.

    USE THIS BEFORE PROPOSING ANY CONCEPT AS TRENDING. Your opinion of what is
    popular is not evidence. If this returns fewer than 2 corroborating sources
    or a score below 0.55, the concept is rejected and you should not build it.
    """
    scored = demand.sweep(
        niche=niche, concept=concept, keywords=keywords, audience=audience
    )
    exp = pipe.validate_and_score(niche, concept, keywords, audience)
    return {
        "scored": {
            "score": scored.score,
            "breakdown": scored.breakdown,
            "verdict": scored.verdict,
            "sources": sorted({s.source for s in scored.signals}),
            "signals": [s.to_dict() for s in scored.signals][:20],
        },
        "accepted": exp is not None,
        "experiment_id": exp.id if exp else None,
    }


@mcp.tool()
def existing_concepts(limit: int = 200) -> list[str]:
    """Concepts already in flight, killed, or scaled. Check before proposing --
    an autonomous loop rediscovers the same trend every day and will otherwise
    rebuild the same shirt indefinitely."""
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT concept, stage FROM experiments ORDER BY updated_at DESC LIMIT %s",
            (limit,),
        )
        return [f"{r['concept']} [{r['stage']}]" for r in cur.fetchall()]


@mcp.tool()
def record_concept(
    niche: str, concept: str, keywords: list[str], audience: str, evidence: list[dict]
) -> dict[str, Any]:
    """Persist a research finding that did NOT clear the threshold, with its
    evidence. Rejected concepts are data: they tell you which niches are
    saturated and let a later re-sweep detect a real breakout."""
    exp = Experiment(niche=niche, concept=concept, keywords=keywords, audience=audience)
    exp.record("recorded_unvalidated", evidence=evidence)
    db.insert_experiment(exp)
    return {"ok": True, "id": exp.id, "note": "stored as DISCOVERED, not scored"}


@mcp.tool()
def amazon_bsr(category: str) -> dict[str, Any]:
    """
    Amazon Best Sellers ranks for a POD category. MUST be run as a browser
    action in your own computer -- Amazon blocks datacenter HTTP instantly and
    serves a captcha, so a raw API call fails in a way that looks like
    'no demand' rather than 'blocked'.

    Return the top 50 ranks and titles. Purchase evidence is the dimension most
    POD automation skips, and it is the one that separates 'people search this'
    from 'people buy this'.
    """
    return {
        "instruction": (
            f"Navigate to {demand.AMAZON_BSR_URLS.get(category, category)} in your "
            "browser. Extract rank, title, price and ASIN for the top 50 entries. "
            "Return them as JSON. If you hit a captcha, report BLOCKED -- do not "
            "report zero results, those mean different things."
        ),
        "urls": demand.AMAZON_BSR_URLS,
    }


# ---------------------------------------------------------------------------
# Design Director / IP Guard
# ---------------------------------------------------------------------------

@mcp.tool()
def screen_ip(
    concept: str,
    title: str,
    description: str,
    design_text: list[str],
    product_type: str = "tshirt",
    image_url: str | None = None,
) -> dict[str, Any]:
    """
    Trademark and copyright clearance. FAILS CLOSED: if the check errors or
    cannot complete, risk is UNKNOWN and the design must not be published.

    Screen BEFORE building, not after. Check the phrase going on the garment
    separately from the listing title and description.

    Note what a LOW verdict does NOT mean: it means no live Class 25 conflict
    was found in the registries checked. It is not a legal opinion, it does not
    cover unregistered common-law marks, and it does not cover copyright in
    artwork. Reverse-image checking the finished art is a separate layer and is
    currently NOT WIRED -- until it is, treat artwork originality as unverified.
    """
    result = trademark.screen(
        concept=concept, title=title, description=description,
        design_text=design_text, product_type=product_type, image_url=image_url,
    )
    if result.risk == "HIGH":
        db.log_event("ip_rejection", result.to_dict())
    return {
        "verdict": "PASS" if result.risk == "LOW" else "REJECT",
        **result.to_dict(),
        "guidance": (
            trademark.phrase_alternatives(concept, result.matches[0].detail)
            if result.matches else ""
        ),
    }


@mcp.tool()
def uspto_lookup(text: str, nice_classes: list[int] | None = None) -> dict[str, Any]:
    """Direct USPTO TESS lookup for one phrase. Use for a targeted second
    opinion when screen_ip returns MEDIUM and you need to see the actual marks."""
    classes = nice_classes or [25]
    matches, err = trademark.layer_uspto([text], classes)
    return {
        "text": text,
        "classes": classes,
        "error": err,
        "matches": [m.__dict__ for m in matches],
        "note": "LIVE and PENDING marks both matter. A pending application can still get you a takedown.",
    }


@mcp.tool()
def record_ip_strike(
    source: str, term: str, detail: dict, experiment_id: str | None = None
) -> dict[str, Any]:
    """
    Record an IP strike: a takedown notice, a cease-and-desist, a platform
    removal. THRESHOLD IS ONE. Recording a strike halts the entire machine
    until a human reviews it. Only call this for a real external claim, not for
    a routine screen rejection (those are logged automatically).
    """
    with db.conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ip_strikes (experiment_id, source, term, detail)
               VALUES (%s,%s,%s,%s)""",
            (experiment_id, source, term, json.dumps(detail, default=str)),
        )
    db.conn.commit()
    halt = sentinel.emergency_halt(
        f"IP strike recorded: {source} / {term!r}. Machine halted pending human review."
    )
    return {"ok": True, "strike_recorded": True, "halt": halt}


@mcp.tool()
def list_styles() -> dict[str, str]:
    """The fixed style library. Pick from these rather than inventing a new
    visual language each time -- staying inside 3-5 styles is what makes a
    storefront read as a brand instead of 500 unrelated AI images."""
    return design.STYLES


@mcp.tool()
def build_design(
    experiment_id: str,
    phrase: str,
    art_subject: str,
    style: str = "retro_sunset",
    layout: str = "arch_top",
    subphrase: str = "",
    palette: str = "",
    product_type: str = "tshirt",
) -> dict[str, Any]:
    """
    Generate artwork and composite the print file.

    The image model renders ART ONLY. `phrase` is typeset by code at 300 DPI
    with a licensed font, so every character is exact. Do not put words in
    art_subject.

    Output: 4500x5400 px transparent PNG at 300 DPI, uploaded to a public URL
    that Printful can fetch. Check `warnings` -- narrow ink coverage or an
    over-tall composition means the design will print poorly even though the
    file is technically valid.
    """
    blocked = _guard()
    if blocked:
        return blocked
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": f"no experiment {experiment_id}"}
    if exp.stage not in (Stage.IP_CLEARED, Stage.DESIGNED):
        return {
            "ok": False,
            "error": f"{experiment_id} is at {exp.stage.value}; IP must clear before design",
        }
    if exp.ip_screen.get("risk") != "LOW":
        return {"ok": False, "error": "IP screen not cleared for this experiment"}

    brief = design.DesignBrief(
        concept=exp.concept, phrase=phrase, subphrase=subphrase, niche=exp.niche,
        style=style, art_subject=art_subject, palette=palette, layout=layout,
        product_type=product_type, audience_language=exp.keywords,
    )
    ok = pipe.build(exp, brief)
    return {
        "ok": ok,
        "experiment_id": experiment_id,
        "stage": exp.stage.value,
        "design_url": exp.design_file_url,
        "spec": getattr(exp, "design_spec", None),
        "prompt": exp.design_prompt,
        "kill_reason": exp.kill_reason,
    }


# ---------------------------------------------------------------------------
# Catalog Bot
# ---------------------------------------------------------------------------

@mcp.tool()
def generate_mockups(
    experiment_id: str,
    product_id: int = DEFAULT_PRODUCT_ID,
    color: str = "black",
) -> dict[str, Any]:
    """
    Generate Printful mockups for the design. Rate limited to 2 tasks/min on a
    new store (10/min once the store has $10 of fulfilled orders), with a
    20,000 file/day cap. Five variants takes ~3 minutes minimum; this is the
    pipeline's slowest step and the client handles the backoff, so do not retry
    on timeout -- a 429 costs a 60-second lockout of the whole endpoint.
    """
    blocked = _guard()
    if blocked:
        return blocked
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": f"no experiment {experiment_id}"}
    variants = (
        DEFAULT_VARIANTS_BLACK if color == "black" else DEFAULT_VARIANTS_BLACK
    )
    ok = pipe.mockups(exp, product_id=product_id, variant_ids=list(variants.values()))
    return {
        "ok": ok,
        "mockups": exp.mockup_urls,
        "stage": exp.stage.value,
        "kill_reason": exp.kill_reason,
    }


@mcp.tool()
def publish_experiment(experiment_id: str, phrase: str) -> dict[str, Any]:
    """
    Publish to Printful, which syncs the listing to Square Online. Prices the
    item from the live Printful base cost plus fees at the configured margin,
    refuses to list if profit falls below the floor, maps Square variations
    back to the experiment for sales attribution, and respects the daily
    listing cap.

    AFTER THIS RETURNS, YOU MUST CALL verify_listing_visible. Square's
    ecom_visibility is read-only via API: a successfully created catalog item
    can still be invisible on the storefront and sell nothing.
    """
    blocked = _guard()
    if blocked:
        return blocked
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": f"no experiment {experiment_id}"}
    ok = pipe.publish(exp, phrase)
    return {
        "ok": ok,
        "stage": exp.stage.value,
        "printful_sync_id": exp.printful_sync_product_id,
        "square_item_id": exp.square_item_id,
        "square_variation_ids": exp.square_variation_ids,
        "retail_price": exp.retail_price,
        "base_cost": exp.base_cost,
        "projected_profit": exp.projected_profit,
        "kill_reason": exp.kill_reason,
        "NEXT_STEP": "call verify_listing_visible -- API success does not mean the item is buyable",
    }


@mcp.tool()
def verify_listing_visible(experiment_id: str, storefront_url: str) -> dict[str, Any]:
    """
    THE BROWSER STEP. Confirm the listing is actually purchasable, then fix it
    if not.

    Ground truth is the public storefront, not the catalog API response. Steps:
      1. Load storefront_url and search for the product title.
      2. If found and add-to-cart works -> record visible, done.
      3. If missing -> Square Dashboard -> Online -> Items -> Site Items ->
         open the item -> Site visibility -> Visible -> Save. Then re-check.

    A human should already have set Dashboard -> Online -> Items -> Item Sync ->
    Item visibility settings -> Visible, which makes new items live by default.
    If you find yourself flipping visibility on every publish, that default is
    not set -- say so in your report instead of silently fixing it 40 times.
    """
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": f"no experiment {experiment_id}"}
    return {
        "ok": True,
        "action_required": "browser",
        "experiment_id": experiment_id,
        "square_item_id": exp.square_item_id,
        "title_to_find": exp.concept,
        "steps": [
            f"Navigate to {storefront_url} and search for the product title.",
            "Confirm the product appears AND has selectable size variants AND an add-to-cart control.",
            "If absent: navigate to https://squareup.com/dashboard/items, open Online > Site Items, find the item, set Site visibility to Visible, Save.",
            "Re-verify on the storefront.",
            "Then call record_visibility with the result.",
        ],
    }


@mcp.tool()
def record_visibility(experiment_id: str, visible: bool, evidence_url: str = "") -> dict[str, Any]:
    """Record the storefront verification result. `visible=false` after a fix
    attempt is an ops alert, not a retry -- three invisible listings in a row
    means the integration is broken and the Catalog Bot cannot fix it."""
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": "unknown experiment"}
    exp.ip_screen["visibility_confirmed"] = visible
    exp.record("visibility_checked", visible=visible, evidence=evidence_url)
    db.save(exp)
    if not visible:
        db.log_event("ops_alert:listing_invisible", {"exp": experiment_id, "url": evidence_url})
    return {"ok": True, "visible": visible}


@mcp.tool()
def reprice_experiment(experiment_id: str, new_retail: float) -> dict[str, Any]:
    """Change retail price. Recomputes profit and refuses to go below the
    floor. Use for a scaling experiment where demand proved stronger than
    expected -- raising price on a proven seller is the highest-leverage
    adjustment available and costs nothing to test."""
    blocked = _guard()
    if blocked:
        return blocked
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": "unknown experiment"}
    profit = ECON.profit_at(new_retail, exp.base_cost)
    if profit < ECON.min_profit:
        return {
            "ok": False,
            "error": f"${new_retail} yields ${profit:.2f} profit, floor is ${ECON.min_profit:.2f}",
        }
    exp.retail_price = round(new_retail, 2)
    exp.projected_profit = round(profit, 2)
    exp.record("repriced", to=new_retail, profit=profit)
    db.save(exp)
    return {"ok": True, "retail": exp.retail_price, "profit": exp.projected_profit,
            "note": "Printful sync variant price must also be updated for the storefront to change"}


@mcp.tool()
def delist_experiment(experiment_id: str, reason: str) -> dict[str, Any]:
    """Remove a listing from Printful and Square. Use for a killed test, an IP
    problem, or a quality complaint."""
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": "unknown experiment"}
    pipe.delist(exp)
    exp.kill(reason)
    db.save(exp)
    return {"ok": True, "delisted": experiment_id}


# ---------------------------------------------------------------------------
# Ops Sentinel
# ---------------------------------------------------------------------------

@mcp.tool()
def check_guardrails() -> dict[str, Any]:
    """
    Evaluate every hard threshold. Returns allowed=true/false with reasons.

    If `hard_halt` is true, call emergency_halt IMMEDIATELY. Do not investigate,
    do not attempt a fix, do not wait for the next scheduled run. Halting is the
    job; diagnosis is a human's job afterward.
    """
    v = sentinel.evaluate()
    return {
        "allowed": v.allowed,
        "hard_halt": v.hard_halt,
        "reasons": v.reasons,
        "spend_7d_fulfillment": db.spend_this_week("fulfillment"),
        "spend_7d_ads": db.spend_this_week("ads"),
        "spend_today_ads": db.spend_today("ads"),
        "listed_today": db.listed_today(),
        "ip_strikes": db.ip_strike_count(),
        "caps": {
            "weekly_fulfillment": GUARDRAILS.weekly_fulfillment_cap,
            "weekly_ads": GUARDRAILS.weekly_ad_cap,
            "daily_ads": GUARDRAILS.daily_ad_cap,
            "listings_per_day": GUARDRAILS.max_new_listings_per_day,
            "concurrent_tests": GUARDRAILS.max_concurrent_tests,
        },
    }


@mcp.tool()
def emergency_halt(reason: str, delist_all: bool = False) -> dict[str, Any]:
    """
    STOP THE MACHINE. Halts every active experiment and blocks all further
    mutating actions until a human resumes.

    delist_all=false (default) halts new activity but leaves live listings up.
    delist_all=true removes every product -- use only for an IP emergency where
    continued sale is itself the liability. Removing 200 listings mid-holiday is
    its own disaster, so this is not the default.

    Restarting is human-only. HALTED experiments never auto-resume.
    """
    result = sentinel.emergency_halt(reason, delist_all=delist_all)
    db.log_event("emergency_halt_invoked", {"reason": reason, "delist_all": delist_all})
    return result


@mcp.tool()
def evaluate_tests() -> dict[str, Any]:
    """
    Render verdicts on every mature test. Maturity is >= 5 days AND >= 800
    impressions; immature tests are HELD, not killed. Killing a design after 40
    impressions is not a decision, it is noise.

    Benchmarks (starting priors -- refit from your own data after ~50
    experiments): CTR >= 0.8%, CVR >= 0.8%, >= 1 order, ROAS >= 1.5 if paid.
    Killed experiments are delisted from both systems automatically.
    """
    blocked = _guard()
    if blocked:
        return blocked
    return pipe.evaluate_tests()


@mcp.tool()
def needs_approval_orders() -> dict[str, Any]:
    """
    Printful orders stuck in NEEDS_APPROVAL: the customer paid and nothing is
    printing. The single most common silent failure in POD automation, caused by
    a product that was not synced at order time or by 'Manually confirm imported
    orders' being enabled in Printful store settings.

    Any count >= 1 is a hard halt. Report the order ids.
    """
    from .printful import PrintfulClient
    try:
        orders = PrintfulClient().needs_approval_orders()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "treat_as": "HALT -- cannot verify the queue"}
    return {
        "count": len(orders),
        "orders": [{"id": o.get("id"), "status": o.get("status"),
                    "created": o.get("created")} for o in orders[:20]],
        "fix": "Printful Dashboard -> Stores -> <store> -> Edit -> Orders -> "
               "turn OFF 'Manually confirm imported orders'. Then Complete order "
               "for anything already stuck.",
    }


@mcp.tool()
def record_ad_spend(experiment_id: str, amount: float, channel: str) -> dict[str, Any]:
    """Record actual ad spend for an experiment. MUST be called with real
    numbers from the ad platform -- the daily cap is enforced against this
    table, so under-reporting here defeats the only ceiling on discretionary
    spend."""
    blocked = _guard()
    if blocked:
        return blocked
    db.record_ad_spend(experiment_id, amount)
    db.log_event("ad_spend", {"exp": experiment_id, "amount": amount, "channel": channel})
    return {"ok": True, "spend_today": db.spend_today("ads"), "cap": GUARDRAILS.daily_ad_cap}


@mcp.tool()
def record_traffic(experiment_id: str, impressions: int = 0, clicks: int = 0, add_to_cart: int = 0) -> dict[str, Any]:
    """Record traffic metrics from the ad platform or storefront analytics. The
    maturity gate needs impressions; without them no test ever becomes decidable
    and everything sits in TESTING forever."""
    db.record_traffic(experiment_id, impressions, clicks, add_to_cart)
    exp = db.get(experiment_id)
    return {"ok": True, "impressions": exp.impressions if exp else 0,
            "clicks": exp.clicks if exp else 0, "mature": exp.test_mature if exp else False}


@mcp.tool()
def daily_report() -> dict[str, Any]:
    """P&L and funnel snapshot. Send this to the owner every day. An autonomous
    system that reports nothing is not autonomous, it is unattended."""
    with db.conn.cursor() as cur:
        cur.execute("SELECT stage, count(*) AS n FROM experiments GROUP BY stage ORDER BY n DESC")
        funnel = {r["stage"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            """SELECT split_part(kill_reason, ':', 1) AS gate, count(*) AS n
               FROM experiments WHERE stage='KILLED' AND kill_reason IS NOT NULL
               GROUP BY 1 ORDER BY n DESC LIMIT 8"""
        )
        kills = {r["gate"]: r["n"] for r in cur.fetchall()}
    return {
        "report": sentinel.daily_report(),
        "pnl": db.pnl_snapshot(),
        "funnel": funnel,
        "top_kill_reasons": kills,
        "guardrails": check_guardrails(),
    }


@mcp.tool()
def pipeline_status() -> dict[str, Any]:
    """Current state of everything. First thing to call when the machine seems
    stuck: `top_kill_reasons` tells you whether your problem is demand (nothing
    scores), IP (everything rejected), or conversion (lists but never sells).
    Those are three different fixes."""
    return daily_report()


def main() -> None:
    log.info("Starting pod-pipeline MCP server (DRY_RUN=%s)", DRY_RUN)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
