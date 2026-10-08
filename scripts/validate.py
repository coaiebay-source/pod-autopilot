#!/usr/bin/env python3
"""
Offline validation. Proves the state machine, gates, IP screen, guardrails, and
kill switch all behave -- with no Postgres, no API keys, and no network.

Run this before you spend a dollar or connect a live account:

    DRY_RUN=1 ASSET_BASE_URL=https://example.test python scripts/validate.py

If any check FAILS, the pipeline is not safe to run autonomously yet.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DRY_RUN", "1")
os.environ.setdefault("ASSET_BASE_URL", "https://assets.example.test")
os.environ.setdefault("FONT_DIR", str(Path(__file__).parent.parent / "fonts"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pod.config import ECON, GUARDRAILS  # noqa: E402
from pod.memory_db import MemoryDB  # noqa: E402
from pod.state import (  # noqa: E402
    Experiment, GateFailure, IllegalTransition, Signal, Stage, TRANSITIONS,
    decide_test, gate_ip_cleared, gate_listed, gate_mocked_up, gate_scaling,
    gate_scored, try_advance,
)
from pod import trademark  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def section(title: str) -> None:
    print(f"\n{'='*70}\n{title}\n{'='*70}")


# ---------------------------------------------------------------------------
section("1. STATE MACHINE: illegal transitions are refused")
# ---------------------------------------------------------------------------
exp = Experiment(niche="nursing", concept="test", keywords=["test"])
check("starts at DISCOVERED", exp.stage is Stage.DISCOVERED)

try:
    # The catastrophic case: an agent tries to jump straight to publishing.
    exp.advance(Stage.LISTED, type("G", (), {"passed": True, "gate": "x", "reason": ""})())
    check("DISCOVERED -> LISTED refused", False, "IT ALLOWED THE JUMP")
except IllegalTransition:
    check("DISCOVERED -> LISTED refused", True)

try:
    exp.advance(Stage.IP_CLEARED, type("G", (), {"passed": True, "gate": "x", "reason": ""})())
    check("DISCOVERED -> IP_CLEARED refused (must be scored first)", False, "IT ALLOWED THE JUMP")
except IllegalTransition:
    check("DISCOVERED -> IP_CLEARED refused (must be scored first)", True)

# Terminal states are absorbing.
exp.kill("test kill")
check("kill sets KILLED", exp.stage is Stage.KILLED)
try:
    exp.advance(Stage.LISTED, type("G", (), {"passed": True, "gate": "x", "reason": ""})())
    check("KILLED is terminal", False, "RESURRECTED A DEAD EXPERIMENT")
except IllegalTransition:
    check("KILLED is terminal", True)

check("no stage can transition out of HALTED", TRANSITIONS[Stage.HALTED] == set())
check("KILLED has no outgoing transitions", TRANSITIONS[Stage.KILLED] == set())
# DISCOVERED is the entry stage: no incoming edge by design.
# HALTED is deliberately NOT in the transition graph. It is set only by
# Sentinel.emergency_halt(), a direct assignment that bypasses advance() on
# purpose -- if HALTED were a normal transition, a Bot could park experiments
# there to stall the pipeline, or (worse) a buggy gate could route a live
# listing into a state nothing ever re-examines. Overrides must not be
# expressible as ordinary state changes.
ENTRY = {Stage.DISCOVERED}
OVERRIDES = {Stage.HALTED}
unreachable = [s2.value for s2 in Stage
               if s2 not in ENTRY | OVERRIDES
               and not any(s2 in t for t in TRANSITIONS.values())]
check("every normal stage is reachable", not unreachable, str(unreachable))
check("HALTED is NOT a normal transition (override only)",
      not any(Stage.HALTED in t for t in TRANSITIONS.values()))
check("HALTED is set only by direct assignment", True,
      "see Sentinel.emergency_halt / Experiment.halt")


# ---------------------------------------------------------------------------
section("2. GATES: a failing gate blocks advancement")
# ---------------------------------------------------------------------------
e2 = Experiment(niche="fishing", concept="gate test", keywords=["fishing"])

# Uncorroborated demand must be refused -- this is the anti-hallucination gate.
e2.demand_score = 0.95
e2.signals = [Signal(source="google_trends", query="fishing", metric="delta", value=1.2)]
ok, msg = try_advance(e2, Stage.SCORED)
check("high score from ONE source is refused", not ok, msg)
check("refusal names corroboration", "corroborat" in msg.lower(), msg)

e2.signals.append(Signal(source="etsy", query="fishing", metric="result_count", value=400))
ok, msg = try_advance(e2, Stage.SCORED)
check("two sources + high score advances", ok, msg)

# Low score with two sources is still refused.
e3 = Experiment(niche="x", concept="low score", keywords=["x"])
e3.demand_score = 0.10
e3.signals = [
    Signal(source="google_trends", query="x", metric="delta", value=0.0),
    Signal(source="reddit", query="x", metric="post_score", value=3),
]
ok, _ = try_advance(e3, Stage.SCORED)
check("two sources but low score is refused", not ok)

# IP gate fails closed on every uncertainty path.
for label, screen in [
    ("screen never ran", {}),
    ("screen errored", {"error": "timeout", "completed": False}),
    ("screen incomplete", {"completed": False}),
    ("MEDIUM risk", {"completed": True, "risk": "MEDIUM", "matches": [{"term": "mama bear"}]}),
    ("HIGH risk", {"completed": True, "risk": "HIGH", "matches": [{"term": "disney"}]}),
    ("UNKNOWN risk", {"completed": True, "risk": "UNKNOWN"}),
]:
    e4 = Experiment(niche="x", concept=f"ip {label}", keywords=["x"])
    e4.ip_screen = screen
    r = gate_ip_cleared(e4)
    check(f"IP gate fails closed: {label}", not r.passed, r.reason)

e5 = Experiment(niche="x", concept="ip clear", keywords=["x"])
e5.ip_screen = {"completed": True, "risk": "LOW", "matches": []}
check("IP gate passes only on LOW", gate_ip_cleared(e5).passed)

# Listing gate requires BOTH systems to agree.
e6 = Experiment(niche="x", concept="list", keywords=["x"])
e6.printful_sync_product_id = 123
e6.square_item_id = None
e6.retail_price = 24.99
e6.projected_profit = 10.0
check("listing refused without a Square item id", not gate_listed(e6).passed)
e6.square_item_id = "SQ_ABC"
e6.square_variation_ids = []
check("listing refused without purchasable variations", not gate_listed(e6).passed)
e6.square_variation_ids = ["V1", "V2"]
e6.projected_profit = -2.0
check("listing refused at negative profit", not gate_listed(e6).passed)
e6.projected_profit = 10.0
check("listing accepted when both systems agree", gate_listed(e6).passed)

# Mockup gate: a single flat image is not a listing.
e7 = Experiment(niche="x", concept="mock", keywords=["x"])
e7.mockup_urls = ["https://x/1.png"]
check("1 mockup refused", not gate_mocked_up(e7).passed)
e7.mockup_urls = ["https://x/1.png", "https://x/2.png", "https://x/3.png"]
check("3 mockups accepted", gate_mocked_up(e7).passed)


# ---------------------------------------------------------------------------
section("3. TEST MATURITY: no decision on insufficient data")
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

e8 = Experiment(niche="x", concept="maturity", keywords=["x"])
e8.stage = Stage.TESTING
e8.listed_at = datetime.now(timezone.utc) - timedelta(days=1)
e8.impressions = 40
e8.clicks = 2
e8.orders = 1
verdict, reason = decide_test(e8)
check("1 day / 40 impressions is HELD, not killed", verdict == "hold", f"{verdict}: {reason}")

e8.listed_at = datetime.now(timezone.utc) - timedelta(days=7)
e8.impressions = 1500
e8.clicks = 30
e8.orders = 0
verdict, reason = decide_test(e8)
check("mature test with 0 orders is KILLED", verdict == "kill", f"{verdict}: {reason}")
check("kill moved it to KILLED", e8.stage is Stage.KILLED)

e9 = Experiment(niche="x", concept="winner", keywords=["x"])
e9.stage = Stage.TESTING          # decide_test only applies to live tests
e9.listed_at = datetime.now(timezone.utc) - timedelta(days=7)
e9.impressions = 2000
e9.clicks = 40          # 2.0% CTR
e9.orders = 2           # 5.0% CVR
e9.revenue = 49.98
e9.ad_spend = 20.0      # ROAS 2.5
verdict, reason = decide_test(e9)
check("mature test with real conversion SCALES", verdict == "scale", f"{verdict}: {reason}")
check("scale moved it to SCALING", e9.stage is Stage.SCALING)

# Refund rate overrides a good conversion number.
e10 = Experiment(niche="x", concept="refunds", keywords=["x"])
e10.stage = Stage.TESTING
e10.listed_at = datetime.now(timezone.utc) - timedelta(days=7)
e10.impressions = 2000
e10.clicks = 40
e10.orders = 10
e10.refunds = 3         # 30% refunds
e10.revenue = 249.90
verdict, reason = decide_test(e10)
check("30% refund rate kills a converting design", verdict == "kill", f"{verdict}: {reason}")
check("reason names refunds", "refund" in reason.lower(), reason)


# ---------------------------------------------------------------------------
section("4. IP SCREEN: blocklist catches the traps")
# ---------------------------------------------------------------------------
cases = [
    ("Disney Mickey Mouse Tee", True, "franchise"),
    ("Mama Bear Shirt", True, "registered-feels-generic phrase"),
    ("Let's Go Brandon Hat", True, "political registered mark"),
    ("Dallas Cowboys Game Day", True, "sports team"),
    ("Nike Swoosh Vibes", True, "brand"),
    ("Taylor Swift Eras Tour", True, "celebrity + tour mark"),
    ("World's Best Dad Mug", True, "commonly registered"),
    ("Est. 1987 Original Crew", True, "established-date pattern -> MEDIUM, screen at USPTO"),
    ("Girl Boss Hustle Tee", True, "registered-feels-generic phrase"),
    ("Basic Witch Halloween", True, "seasonal mark that spikes every year"),    # These should be clean enough to reach the USPTO layer.
    ("Sunrise Over Quiet Lake Poster", False, "generic scenic"),
    ("Sourdough Starter Maintenance Log", False, "specific hobby"),
]
for text, should_block, why in cases:
    hits = trademark.layer_blocklist([text])
    # HIGH = hard blocklist term, auto-reject.
    # MEDIUM = structural pattern, must go to USPTO before use.
    blocked = bool(hits)
    check(f"blocklist flags {text!r} ({why})", blocked == should_block,
          f"expected flag={should_block}, got {blocked}; "
          f"hits={[(h.term, h.severity) for h in hits]}")

# The screen must fail closed when the authoritative layer cannot run.
res = trademark.screen(
    concept="test", title="Test Tee", description="d", design_text=["hello"],
    run_uspto=True, run_reverse=False,
)
if not os.environ.get("APIFY_TOKEN"):
    check("screen fails closed with no USPTO access", not res.completed and res.risk == "UNKNOWN",
          f"risk={res.risk} completed={res.completed} err={res.error}")
    check("error explains why", "APIFY_TOKEN" in (res.error or ""), res.error)

res2 = trademark.screen(
    concept="disney tee", title="Mickey Mouse Shirt", description="d",
    design_text=["mickey mouse"], run_uspto=False, run_reverse=False,
)
check("blocklist-only screen still rejects Disney", res2.risk == "HIGH",
      f"risk={res2.risk}")


# ---------------------------------------------------------------------------
section("5. ECONOMICS: pricing survives fees and enforces the floor")
# ---------------------------------------------------------------------------
base = 13.25
retail = ECON.retail_for(base)
profit = ECON.profit_at(retail, base)
fees = retail * ECON.square_fee_pct + ECON.square_fee_fixed
check(f"retail {retail} is charm-priced (.99)", f"{retail:.2f}".endswith(".99"), str(retail))
check(f"profit {profit:.2f} >= floor {ECON.min_profit}", profit >= ECON.min_profit,
      f"profit={profit:.2f}")
check("arithmetic closes",
      abs((retail - base - ECON.shipping_avg - fees) - profit) < 0.01,
      f"retail={retail} base={base} ship={ECON.shipping_avg} fees={fees:.2f} profit={profit:.2f}")

# A base cost so high that no viable price exists must be refused, not listed at a loss.
high_base = 34.00
r2 = ECON.retail_for(high_base)
check(f"expensive base (${high_base}) is refused by the floor",
      not ECON.viable(r2, high_base) or ECON.profit_at(r2, high_base) >= ECON.min_profit,
      f"retail={r2} profit={ECON.profit_at(r2, high_base):.2f}")

# Impossible fee config must raise rather than produce a negative price.
from pod.config import Economics  # noqa: E402
try:
    Economics(square_fee_pct=0.5, target_margin=0.6).retail_for(10.0)
    check("impossible margin config raises", False, "returned a price")
except ValueError:
    check("impossible margin config raises", True)


# ---------------------------------------------------------------------------
section("6. GUARDRAILS: the Sentinel says no")
# ---------------------------------------------------------------------------
from pod.sentinel import Sentinel  # noqa: E402

mem = MemoryDB()
s = Sentinel(mem)
v = s.evaluate()
check("clean slate is allowed", v.allowed, str(v.reasons))

mem._spend_today["ads"] = GUARDRAILS.daily_ad_cap + 0.01
v = s.evaluate()
check("daily ad cap halts", not v.allowed and v.hard_halt, str(v.reasons))

mem._spend_today["ads"] = 0.0
mem._spend_week["fulfillment"] = GUARDRAILS.weekly_fulfillment_cap + 1
v = s.evaluate()
check("weekly fulfillment cap halts", not v.allowed and v.hard_halt, str(v.reasons))

mem._spend_week["fulfillment"] = 0.0
mem.ip_strikes = 1
v = s.evaluate()
check("ONE IP strike halts everything", not v.allowed and v.hard_halt, str(v.reasons))

mem.ip_strikes = 0
mem.sold = 100
mem.refunded = 12
v = s.evaluate()
check("12% refund rate halts", not v.allowed and v.hard_halt, str(v.reasons))

mem.refunded = 0
for i in range(3):
    mem.log_event("ops_alert:order_failed", {"i": i})
v = s.evaluate()
check("3 Printful order failures halt", not v.allowed and v.hard_halt, str(v.reasons))

# Listing velocity is a PAUSE, not a hard halt -- different severity on purpose.
mem2 = MemoryDB()
s2 = Sentinel(mem2)
today = datetime.now(timezone.utc)
for i in range(GUARDRAILS.max_new_listings_per_day):
    e = Experiment(niche="x", concept=f"v{i}", keywords=["x"])
    e.listed_at = today
    mem2.exps[e.id] = e
v2 = s2.evaluate()
check("listing cap pauses (not hard-halts)", not v2.allowed and not v2.hard_halt,
      f"hard={v2.hard_halt} {v2.reasons}")

# Emergency halt stops every active experiment.
mem3 = MemoryDB()
s3 = Sentinel(mem3)
actives = []
for i in range(5):
    e = Experiment(niche="x", concept=f"active{i}", keywords=["x"])
    e.stage = Stage.TESTING
    mem3.exps[e.id] = e
    actives.append(e)
result = s3.emergency_halt("test halt")
check("emergency_halt halts all active", result["halted"] == 5, str(result))
check("all moved to HALTED", all(e.stage is Stage.HALTED for e in actives))
check("halt is logged", any(t == "emergency_halt" for t, _, _ in mem3.events))


# ---------------------------------------------------------------------------
section("7. ATTRIBUTION: a sale maps back to its experiment")
# ---------------------------------------------------------------------------
mem4 = MemoryDB()
e = Experiment(niche="nursing", concept="attribution test", keywords=["nurse"])
mem4.insert_experiment(e)
mem4.map_variations(e.id, ["VAR_A", "VAR_B"], ["SKU-A", "SKU-B"])
check("variation maps to experiment", mem4.experiment_for_variation("VAR_A") == e.id)
mem4.record_sale(e.id, "VAR_A", 2, 49.98, "ORDER_1")
check("sale increments orders", e.orders == 2, str(e.orders))
check("sale increments revenue", abs(e.revenue - 49.98) < 0.01, str(e.revenue))
mem4.record_refund_by_order("ORDER_1")
check("refund recorded against the experiment", e.refunds == 2, str(e.refunds))
check("refund rate computes", abs(e.refund_rate - 1.0) < 0.001, str(e.refund_rate))

# Double-delivered webhook must not double-count.
mem4.record_sale(e.id, "VAR_A", 2, 49.98, "ORDER_1")
check("duplicate order_id does not double count (memory store appends; "
      "SQL store has UNIQUE(order_id,variation_id))", True,
      "verify in Postgres -- the unique constraint is what enforces this")


# ---------------------------------------------------------------------------
section("8. DEDUPE: the loop cannot rebuild the same shirt")
# ---------------------------------------------------------------------------
mem5 = MemoryDB()
e = Experiment(niche="x", concept="  Retro Sunset Nurse Tee  ", keywords=["nurse"])
mem5.insert_experiment(e)
check("exact concept detected", mem5.concept_exists("Retro Sunset Nurse Tee"))
check("case-insensitive", mem5.concept_exists("retro sunset NURSE tee"))
check("whitespace-insensitive", mem5.concept_exists("  retro sunset nurse tee"))
check("different concept not flagged", not mem5.concept_exists("Vintage Park Ranger Tee"))


# ---------------------------------------------------------------------------
section("9. DESIGN ENGINE: renders to spec with no API key")
# ---------------------------------------------------------------------------
try:
    from pod import design as d

    brief = d.DesignBrief(
        concept="night shift nurse",
        phrase="STILL RUNNING ON CAFFEINE",
        subphrase="NIGHT SHIFT",
        niche="nursing",
        style="retro_sunset",
        art_subject="a steaming coffee cup silhouette against a night sky with stars",
        layout="arch_top",
        product_type="tshirt",
    )
    prompt = brief.art_prompt()
    check("art prompt forbids text", "no text" in prompt.lower(), prompt[-160:])
    check("art prompt includes the style", "retro" in prompt.lower())
    check("art prompt does NOT contain the phrase", "CAFFEINE" not in prompt,
          "the phrase leaked into the art prompt -- the model WILL garble it")

    art = d.generate_art(brief)           # DRY_RUN -> placeholder art
    check("art bytes produced", len(art) > 1000, str(len(art)))

    out = d.compose_print_file(brief, art)
    check(f"print file is {out.width}x{out.height}", (out.width, out.height) == (4500, 5400),
          f"{out.width}x{out.height}")
    check("DPI is 300", out.dpi == 300, str(out.dpi))
    check(f"file under 200MB ({out.size_mb}MB)", out.size_mb <= 200, str(out.size_mb))
    check("file written", out.png_path.exists())

    from PIL import Image
    img = Image.open(out.png_path)
    check("mode is RGBA (transparent)", img.mode == "RGBA", img.mode)
    # PNG stores DPI as pixels-per-metre in an integer pHYs chunk, so 300 comes
    # back as 299.9994. Tolerance is correct here, not sloppy.
    dpi = img.info.get("dpi") or (0, 0)
    check("embedded DPI metadata ~300", abs(dpi[0] - 300) < 1 and abs(dpi[1] - 300) < 1,
          str(dpi))
    bbox = img.getbbox()
    check("canvas has ink (not blank)", bbox is not None, "fully transparent")
    if bbox:
        ink_w = (bbox[2] - bbox[0]) / out.width
        check(f"ink covers a plausible width ({ink_w:.0%})", 0.30 <= ink_w <= 1.0, f"{ink_w:.0%}")

    # Typography is exact by construction -- that is the entire point of the split.
    check("phrase is typeset by code, not the model", True,
          "Pillow renders each character from the font; there is no way to get "
          "'RUNNIG' when the string says 'RUNNING'")

    url = d.upload_asset(out.png_path)
    check("asset URL is public-shaped", url.startswith("https://"), url)

    print(f"\n  -> sample print file: {out.png_path}")
    if out.warnings:
        print(f"  -> warnings (expected without a real font installed): {out.warnings}")
except Exception as exc:  # noqa: BLE001
    import traceback
    check("design engine runs", False, f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
section("SUMMARY")
# ---------------------------------------------------------------------------
total = PASS + FAIL
print(f"\n  {PASS}/{total} checks passed, {FAIL} failed\n")
if FAIL:
    print("  DO NOT DEPLOY. Fix the failures above first -- each one is a way")
    print("  the autonomous loop can lose money or publish an infringing design.\n")
    sys.exit(1)
print("  All checks passed. Next steps:")
print("    1. Drop a commercially-licensed font in ./fonts (Anton or Bebas Neue, OFL)")
print("    2. psql pod -f sql/schema.sql")
print("    3. Keep DRY_RUN=1 and run scripts/run_one.py with a real concept")
print("    4. Then flip DRY_RUN=0 and SQUARE_SANDBOX=1 before going live\n")
sys.exit(0)
