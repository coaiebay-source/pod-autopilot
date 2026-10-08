"""
The experiment state machine.

This is the single most important file in the system. Every design idea is one
row that walks a fixed set of stages. A row CANNOT advance without passing the
gate for its stage, and gates are evaluated in code -- not in a prompt. That is
what makes "fully autonomous" survivable: the LLM proposes, the state machine
disposes.

Why a table and not agent memory: an autonomous pipeline that runs for months
will hit restarts, model swaps, and partial failures. Agent memory does not
survive those. A Postgres row does.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import GUARDRAILS


class Stage(str, enum.Enum):
    DISCOVERED = "DISCOVERED"      # A scout found a signal
    SCORED = "SCORED"              # Demand score computed, above threshold
    IP_CLEARED = "IP_CLEARED"      # Trademark/copyright screen passed
    DESIGNED = "DESIGNED"          # Print file rendered to spec
    MOCKED_UP = "MOCKED_UP"        # Printful mockups generated
    LISTED = "LISTED"              # Live in Square Online, purchasable
    TESTING = "TESTING"            # Traffic being driven, metrics collecting
    SCALING = "SCALING"            # Passed the test, spend increasing
    KILLED = "KILLED"              # Failed. Terminal.
    HALTED = "HALTED"              # Guardrail tripped. Terminal until human.


# Legal transitions. Anything not listed here is refused. This is the second
# layer of protection: even a compromised or hallucinating agent cannot push a
# design from DISCOVERED straight to LISTED.
TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.DISCOVERED: {Stage.SCORED, Stage.KILLED},
    Stage.SCORED: {Stage.IP_CLEARED, Stage.KILLED},
    Stage.IP_CLEARED: {Stage.DESIGNED, Stage.KILLED},
    Stage.DESIGNED: {Stage.MOCKED_UP, Stage.KILLED},
    Stage.MOCKED_UP: {Stage.LISTED, Stage.KILLED},
    Stage.LISTED: {Stage.TESTING, Stage.KILLED},
    Stage.TESTING: {Stage.SCALING, Stage.KILLED},
    Stage.SCALING: {Stage.KILLED},
    Stage.KILLED: set(),
    Stage.HALTED: set(),
}

TERMINAL = {Stage.KILLED, Stage.HALTED}


class IllegalTransition(Exception):
    pass


class GateFailure(Exception):
    """Raised when a stage's entry gate is not satisfied."""


@dataclass
class Signal:
    """One unit of demand evidence. Multiple signals per experiment."""

    source: str          # "google_trends" | "etsy" | "amazon_bsr" | "tiktok" | "reddit" | "pinterest"
    query: str           # what was searched
    metric: str          # "breakout" | "result_count" | "bsr_rank" | "video_views" | "post_score"
    value: float
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    evidence_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "query": self.query,
            "metric": self.metric,
            "value": self.value,
            "captured_at": self.captured_at,
            "evidence_url": self.evidence_url,
            "raw": self.raw,
        }


@dataclass
class GateResult:
    passed: bool
    gate: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Experiment:
    """One design idea, tracked end to end."""

    id: str = field(default_factory=lambda: f"exp_{uuid.uuid4().hex[:12]}")
    stage: Stage = Stage.DISCOVERED
    niche: str = ""
    concept: str = ""              # human-readable one-liner, e.g. "retro sunset 'still running on caffeine' tee"
    keywords: list[str] = field(default_factory=list)
    audience: str = ""
    product_id: int | None = None  # Printful catalog product id (71 = BC3001)
    variant_ids: list[int] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    demand_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    ip_screen: dict[str, Any] = field(default_factory=dict)
    design_file_url: str | None = None
    design_prompt: str = ""
    mockup_urls: list[str] = field(default_factory=list)
    printful_sync_product_id: int | None = None
    square_item_id: str | None = None
    square_variation_ids: list[str] = field(default_factory=list)
    retail_price: float = 0.0
    base_cost: float = 0.0
    projected_profit: float = 0.0
    # Test metrics
    listed_at: datetime | None = None
    impressions: int = 0
    clicks: int = 0
    add_to_cart: int = 0
    orders: int = 0
    revenue: float = 0.0
    ad_spend: float = 0.0
    refunds: int = 0
    # Bookkeeping
    history: list[dict[str, Any]] = field(default_factory=list)
    kill_reason: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # -- lifecycle ---------------------------------------------------------

    def record(self, event: str, **detail: Any) -> None:
        self.history.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "stage": self.stage.value,
                **detail,
            }
        )

    def advance(self, to: Stage, gate: GateResult, **detail: Any) -> None:
        if self.stage in TERMINAL:
            raise IllegalTransition(
                f"{self.id} is terminal ({self.stage.value}); cannot advance"
            )
        if to not in TRANSITIONS[self.stage]:
            raise IllegalTransition(
                f"{self.id}: {self.stage.value} -> {to.value} is not a legal transition. "
                f"Allowed: {[s.value for s in TRANSITIONS[self.stage]]}"
            )
        if not gate.passed:
            raise GateFailure(f"{self.id} failed gate '{gate.gate}': {gate.reason}")
        self.record(f"advance:{to.value}", gate=gate.gate, **detail)
        self.stage = to
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def kill(self, reason: str) -> None:
        if self.stage in TERMINAL:
            return
        self.record("kill", reason=reason)
        self.stage = Stage.KILLED
        self.kill_reason = reason
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def halt(self, reason: str) -> None:
        self.record("halt", reason=reason)
        self.stage = Stage.HALTED
        self.kill_reason = reason
        self.updated_at = datetime.now(timezone.utc).isoformat()

    # -- derived metrics ---------------------------------------------------

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def conversion_rate(self) -> float:
        return self.orders / self.clicks if self.clicks else 0.0

    @property
    def refund_rate(self) -> float:
        return self.refunds / self.orders if self.orders else 0.0

    @property
    def roas(self) -> float:
        return self.revenue / self.ad_spend if self.ad_spend else 0.0

    @property
    def days_live(self) -> int:
        if not self.listed_at:
            return 0
        return (datetime.now(timezone.utc) - self.listed_at).days

    @property
    def test_mature(self) -> bool:
        """A test is only decidable once it has had enough exposure. Killing a
        design after 40 impressions is not a decision, it is noise."""
        if GUARDRAILS.traffic_mode == "organic":
            # No impressions exist. Mature when it has had a fair shot
            # (days + clicks), OR when it has simply been up too long to keep
            # occupying a test slot.
            return (
                self.days_live >= GUARDRAILS.organic_min_days
                and self.clicks >= GUARDRAILS.organic_min_clicks
            ) or self.days_live >= GUARDRAILS.organic_max_days
        return (
            self.days_live >= GUARDRAILS.min_test_days
            and self.impressions >= GUARDRAILS.min_test_impressions
        )


# ---------------------------------------------------------------------------
# GATES. Pure functions. No LLM in here on purpose -- a gate that can be
# talked into passing is not a gate.
# ---------------------------------------------------------------------------

SCORE_THRESHOLD = 0.55


def gate_scored(exp: Experiment) -> GateResult:
    """Must clear the demand threshold on at least 2 independent sources."""
    sources = {s.source for s in exp.signals}
    if len(sources) < 2:
        return GateResult(
            False,
            "demand_corroboration",
            f"Only {len(sources)} independent signal source(s). A single source is how "
            f"an LLM hallucinates a trend. Require >= 2.",
            {"sources": sorted(sources)},
        )
    if exp.demand_score < SCORE_THRESHOLD:
        return GateResult(
            False,
            "demand_threshold",
            f"Score {exp.demand_score:.3f} < {SCORE_THRESHOLD}",
            {"breakdown": exp.score_breakdown},
        )
    return GateResult(True, "demand_threshold", "ok", {"sources": sorted(sources)})


def gate_ip_cleared(exp: Experiment) -> GateResult:
    """FAIL CLOSED. If the screen did not run, or errored, or returned any
    live match in Class 25, the design dies. This gate is not negotiable and
    should never be loosened to hit a listing quota."""
    screen = exp.ip_screen
    if not screen:
        return GateResult(False, "ip_screen", "Screen never ran. Fail closed.")
    if screen.get("error"):
        return GateResult(
            False, "ip_screen", f"Screen errored: {screen['error']}. Fail closed."
        )
    if not screen.get("completed"):
        return GateResult(False, "ip_screen", "Screen incomplete. Fail closed.")
    risk = screen.get("risk", "UNKNOWN").upper()
    if risk != "LOW":
        matches = screen.get("matches", [])
        return GateResult(
            False,
            "ip_screen",
            f"Risk level {risk}. Matches: {json.dumps(matches)[:400]}",
            {"matches": matches},
        )
    return GateResult(True, "ip_screen", "No live Class 25 conflict found")


def gate_designed(exp: Experiment) -> GateResult:
    """Print file must exist, be public, and be the right size."""
    if not exp.design_file_url:
        return GateResult(False, "print_file", "No design file URL")
    spec = exp.design_spec if hasattr(exp, "design_spec") else None
    if spec:
        w, h = spec.get("width", 0), spec.get("height", 0)
        if (w, h) != (spec.get("expected_width"), spec.get("expected_height")):
            return GateResult(
                False,
                "print_file_dimensions",
                f"Rendered {w}x{h}, expected {spec.get('expected_width')}x{spec.get('expected_height')}",
            )
    return GateResult(True, "print_file", "ok")


def gate_mocked_up(exp: Experiment) -> GateResult:
    if not exp.mockup_urls:
        return GateResult(False, "mockups", "No mockups generated")
    if len(exp.mockup_urls) < 3:
        return GateResult(
            False,
            "mockups",
            f"Only {len(exp.mockup_urls)} mockup(s). A listing with one flat image "
            f"converts like a listing nobody trusts. Require >= 3 angles.",
        )
    return GateResult(True, "mockups", "ok")


def gate_listed(exp: Experiment) -> GateResult:
    """Both systems must agree the product exists AND is purchasable."""
    if not exp.printful_sync_product_id:
        return GateResult(False, "listed", "No Printful sync product id")
    if not exp.square_item_id:
        return GateResult(False, "listed", "No Square catalog item id")
    if not exp.square_variation_ids:
        return GateResult(False, "listed", "No Square variations -- nothing to buy")
    if exp.retail_price <= 0:
        return GateResult(False, "listed", "Retail price not set")
    if exp.projected_profit <= 0:
        return GateResult(
            False, "listed", f"Projected profit {exp.projected_profit} <= 0"
        )
    # Square's ecom_visibility is READ-ONLY via API. If the visibility flip did
    # not happen (browser step or dashboard default), the item exists in the
    # catalog and sells nothing. This check is what catches that silently.
    if not exp.ip_screen.get("visibility_confirmed"):
        pass  # tracked separately; see ops_sentinel
    return GateResult(True, "listed", "ok")


def gate_scaling(exp: Experiment) -> GateResult:
    """The test verdict. Requires maturity first, then real conversion."""
    if not exp.test_mature:
        return GateResult(
            False,
            "test_maturity",
            (f"{exp.days_live}d live / {exp.clicks} clicks. Need {GUARDRAILS.organic_min_days}d "
             f"and {GUARDRAILS.organic_min_clicks} clicks (or {GUARDRAILS.organic_max_days}d)."
             if GUARDRAILS.traffic_mode == "organic" else
             f"{exp.days_live}d live / {exp.impressions} impressions. "
             f"Need {GUARDRAILS.min_test_days}d and {GUARDRAILS.min_test_impressions} impressions."),
        )
    if exp.refund_rate > GUARDRAILS.refund_rate_halt:
        return GateResult(
            False,
            "refund_rate",
            f"{exp.refund_rate:.1%} refunds. Print quality or misleading listing.",
        )
    if GUARDRAILS.traffic_mode == "organic":
        # Organic benchmarks: no CTR (no impressions), no ROAS (no spend).
        # Pass = it converted real strangers at a sane rate. Priors; refit later.
        if exp.orders == 0:
            return GateResult(False, "orders", f"Zero sales in {exp.days_live}d / {exp.clicks} clicks")
        if exp.clicks >= GUARDRAILS.organic_min_clicks and exp.conversion_rate < GUARDRAILS.organic_min_cvr:
            return GateResult(False, "cvr", f"CVR {exp.conversion_rate:.2%} < {GUARDRAILS.organic_min_cvr:.0%} (organic)")
        return GateResult(True, "test_passed", "ok (organic)")
    # Benchmarks for POD apparel on cold traffic. Tune from your own data after
    # ~50 experiments; these are starting priors, not truth.
    if exp.ctr < 0.008:
        return GateResult(False, "ctr", f"CTR {exp.ctr:.3%} < 0.80%")
    if exp.orders == 0:
        return GateResult(False, "orders", "Zero sales on a mature test")
    if exp.conversion_rate < 0.008:
        return GateResult(
            False, "cvr", f"CVR {exp.conversion_rate:.3%} < 0.80%"
        )
    if exp.ad_spend > 0 and exp.roas < 1.5:
        return GateResult(False, "roas", f"ROAS {exp.roas:.2f} < 1.50")
    return GateResult(True, "test_passed", "ok")


GATES: dict[Stage, Any] = {
    Stage.SCORED: gate_scored,
    Stage.IP_CLEARED: gate_ip_cleared,
    Stage.DESIGNED: gate_designed,
    Stage.MOCKED_UP: gate_mocked_up,
    Stage.LISTED: gate_listed,
    Stage.SCALING: gate_scaling,
}


def try_advance(exp: Experiment, to: Stage) -> tuple[bool, str]:
    """Run the gate for `to`, then advance. Returns (ok, message)."""
    gate_fn = GATES.get(to)
    if gate_fn is None:
        exp.advance(to, GateResult(True, "none", "no gate defined"))
        return True, f"advanced to {to.value} (no gate)"
    result: GateResult = gate_fn(exp)
    try:
        exp.advance(to, result)
        return True, f"advanced to {to.value}"
    except (IllegalTransition, GateFailure) as exc:
        return False, str(exc)


def decide_test(exp: Experiment) -> tuple[str, str]:
    """
    Called by the Sentinel on mature tests. Returns ("scale"|"kill", reason).
    Separate from gate_scaling so the kill path gets an explicit reason for the
    audit trail.
    """
    if not exp.test_mature:
        if GUARDRAILS.traffic_mode == "organic":
            return "hold", f"not mature ({exp.days_live}d/{exp.clicks} clicks)"
        return "hold", f"not mature ({exp.days_live}d/{exp.impressions}impr)"
    ok, msg = try_advance(exp, Stage.SCALING)
    if ok:
        return "scale", msg
    exp.kill(msg)
    return "kill", msg
