"""
The Sentinel: guardrails and the kill switch.

A fully autonomous system that spends your money needs a component whose only
job is to say NO. That is this module. It runs before any action that creates
liability or spend, and on a timer.

It is deliberately dumb. No LLM, no judgment, no "in this case it seems fine."
Numeric thresholds, checked against the database, returning a boolean. If it
cannot determine the answer, the answer is no.

Trip any hard threshold and the machine enters HALTED: no new listings, no
ad spend, existing listings optionally delisted. Restarting requires a human
to clear the condition. This is not pessimism -- it is the only thing standing
between an automated design pipeline and a trademark lawsuit or a $4,000
Printful bill from a retry loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import DRY_RUN, GUARDRAILS
from .db import DB
from .state import Experiment, Stage

log = logging.getLogger("pod.sentinel")


@dataclass
class Verdict:
    allowed: bool
    reasons: list[str]
    hard_halt: bool = False

    def __bool__(self) -> bool:
        return self.allowed


class Sentinel:
    def __init__(self, db: DB | None = None):
        self.db = db or DB()

    # ------------------------------------------------------------------
    # Pre-action gate. Call before ANYTHING that costs money or creates
    # public liability.
    # ------------------------------------------------------------------
    def may_proceed(self) -> tuple[bool, str]:
        v = self.evaluate()
        if v.allowed:
            return True, "clear"
        return False, "; ".join(v.reasons)

    def evaluate(self) -> Verdict:
        reasons: list[str] = []
        hard = False

        # 1. IP strikes. One takedown or cease-and-desist stops everything.
        strikes = self.db.ip_strike_count()
        if strikes >= GUARDRAILS.ip_strikes_halt:
            reasons.append(
                f"HALT: {strikes} IP strike(s) on record "
                f"(threshold {GUARDRAILS.ip_strikes_halt}). Do not ship another "
                "design until the notice is reviewed by a human."
            )
            hard = True

        # 2. Weekly fulfillment spend. A retry loop that re-submits orders, or
        #    a bot that starts approving its own test purchases, burns money
        #    with no revenue. This is the check that catches it.
        ful = self.db.spend_this_week("fulfillment")
        if ful >= GUARDRAILS.weekly_fulfillment_cap:
            reasons.append(
                f"HALT: weekly fulfillment spend ${ful:.2f} >= cap "
                f"${GUARDRAILS.weekly_fulfillment_cap:.2f}"
            )
            hard = True

        # 3. Ad spend, daily and weekly.
        ad_today = self.db.spend_today("ads")
        if ad_today >= GUARDRAILS.daily_ad_cap:
            reasons.append(
                f"HALT: ad spend today ${ad_today:.2f} >= daily cap "
                f"${GUARDRAILS.daily_ad_cap:.2f}"
            )
            hard = True
        ad_week = self.db.spend_this_week("ads")
        if ad_week >= GUARDRAILS.weekly_ad_cap:
            reasons.append(
                f"HALT: weekly ad spend ${ad_week:.2f} >= cap "
                f"${GUARDRAILS.weekly_ad_cap:.2f}"
            )
            hard = True

        # 4. Listing velocity. 200 new shirts a day is not a store, it is spam,
        #    and Square will notice.
        listed = self.db.listed_today()
        if listed >= GUARDRAILS.max_new_listings_per_day:
            reasons.append(
                f"PAUSE: {listed} listings created today >= cap "
                f"{GUARDRAILS.max_new_listings_per_day}"
            )

        # 5. Concurrent tests. Each test costs attention and money; too many at
        #    once means none of them reaches significance.
        testing = self.db.count_staged(Stage.TESTING)
        if testing >= GUARDRAILS.max_concurrent_tests:
            reasons.append(
                f"PAUSE: {testing} concurrent tests >= cap "
                f"{GUARDRAILS.max_concurrent_tests}. Resolve existing tests first."
            )

        # 6. Global refund rate. Above ~8% you have a product problem, and
        #    scaling makes it worse.
        refund_rate = self._global_refund_rate()
        if refund_rate is not None and refund_rate > GUARDRAILS.refund_rate_halt:
            reasons.append(
                f"HALT: refund rate {refund_rate:.1%} > "
                f"{GUARDRAILS.refund_rate_halt:.0%}. Print quality or listing "
                "accuracy problem -- stop selling until diagnosed."
            )
            hard = True

        # 7. Unattended fulfillment failures. Printful order_failed events mean
        #    customers paid and got nothing.
        failures = self.db.recent_events("ops_alert:order_failed", hours=48)
        if len(failures) >= 3:
            reasons.append(
                f"HALT: {len(failures)} Printful order failures in 48h. "
                "Customers have paid for products that cannot be made."
            )
            hard = True

        # 8. Orders stuck in NEEDS_APPROVAL. The most common silent failure in
        #    Printful<->store automation.
        stuck = self._needs_approval_count()
        if stuck >= 1:
            reasons.append(
                f"HALT: {stuck} Printful order(s) in NEEDS_APPROVAL. A customer "
                "has paid and nothing is being printed. Check Stores -> Orders "
                "-> Needs approval, and verify 'Manually confirm imported "
                "orders' is OFF in Printful store settings."
            )
            hard = True

        if reasons:
            log.warning("Sentinel: %s", " | ".join(reasons))
        return Verdict(allowed=not reasons, reasons=reasons, hard_halt=hard)

    def _global_refund_rate(self) -> float | None:
        try:
            with self.db.conn.cursor() as cur:
                cur.execute(
                    """SELECT COALESCE(SUM(qty),0) AS sold,
                              COALESCE(SUM(qty) FILTER (WHERE refunded),0) AS refunded
                         FROM sales WHERE at > now() - interval '30 days'"""
                )
                row = cur.fetchone()
            sold = int(row["sold"] or 0)
            if sold < 10:
                return None  # too little data to act on
            return int(row["refunded"] or 0) / sold
        except Exception:  # noqa: BLE001
            # Cannot determine -> fail closed. A sentinel that shrugs is worse
            # than no sentinel, because you believe you are protected.
            return 1.0

    def _needs_approval_count(self) -> int:
        if DRY_RUN:
            return 0
        try:
            from .printful import PrintfulClient
            return len(PrintfulClient().needs_approval_orders())
        except Exception as exc:  # noqa: BLE001
            log.error("Cannot check Printful approval queue: %s", exc)
            return 1  # unknown -> assume stuck -> halt

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------
    def emergency_halt(self, reason: str, delist_all: bool = False) -> dict[str, Any]:
        """
        Stop the machine. Optionally pull every listing.

        Wire this to:
          * a physical button (a Slack command, an SMS reply, a URL you bookmark)
          * the hard-halt path above
          * a manual invocation when something looks wrong that no threshold caught

        Delisting everything is reversible but slow (you must rebuild). Default
        is to halt new activity and leave live listings up, because an innocent
        halt that removes 200 products mid-holiday is its own disaster.
        """
        affected = []
        for exp in self.db.active():
            exp.halt(f"emergency halt: {reason}")
            if delist_all:
                try:
                    from .pipeline import Pipeline
                    Pipeline(self.db).delist(exp)
                except Exception as exc:  # noqa: BLE001
                    log.error("Delist failed for %s: %s", exp.id, exc)
            self.db.save(exp)
            affected.append(exp.id)

        self.db.log_event("emergency_halt", {"reason": reason, "n": len(affected),
                                             "delisted": delist_all})
        log.critical("EMERGENCY HALT: %s (%d experiments halted)", reason, len(affected))
        return {"halted": len(affected), "ids": affected, "reason": reason,
                "delisted": delist_all,
                "at": datetime.now(timezone.utc).isoformat()}

    def resume(self, note: str = "") -> dict[str, Any]:
        """
        Human-in-the-loop restart. HALTED experiments do NOT auto-resume --
        they were halted for a reason, and the reason needs a person to look at
        it. This moves them back to KILLED so they leave the active set, and
        clears the halt flag by logging an explicit human decision.
        """
        n = 0
        for exp in self.db.by_stage(Stage.HALTED):
            exp.kill(f"closed after halt review: {note}")
            self.db.save(exp)
            n += 1
        self.db.log_event("halt_resumed_by_human", {"note": note, "closed": n})
        return {"closed": n}

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def daily_report(self) -> str:
        p = self.db.pnl_snapshot()
        v = self.evaluate()
        lines = [
            "=== POD AUTOPILOT DAILY REPORT ===",
            f"Revenue (all time):  ${p['revenue']:.2f}",
            f"COGS (fulfillment):  ${p['cogs']:.2f}",
            f"Ad spend:            ${p['ads']:.2f}",
            f"Square fees (est):   ${p['square_fees_est']:.2f}",
            f"NET:                 ${p['net']:.2f}",
            "",
            f"Experiments: {p['total']} total | {p['scaling']} scaling | "
            f"{p['killed']} killed | {p['selling_designs']} with sales",
            "",
            f"Guardrails: {'CLEAR' if v.allowed else 'BLOCKED'}",
        ]
        lines += [f"  - {r}" for r in v.reasons]
        return "\n".join(lines)
