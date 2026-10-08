"""
Scheduled entrypoints for the ZERO-CASH deployment.

In the paid plan, OpenBot ran five Bots on routines. Here the scheduler is
GitHub Actions cron (free minutes) and the "bots" collapse into two jobs:

    python -m pod.batch discover   # daily: raw signals -> LLM -> candidates.json
    python -m pod.batch design     # daily: candidates.json -> score/IP/design/list
    python -m pod.batch ops        # every 30 min: poll orders, clicks, verdicts, sentinel
    python -m pod.batch review     # weekly: plain-English report (LLM) -> REPORT.md

Same state machine, same gates, same Sentinel. The LLM still only proposes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pod.batch")

DEFAULT_NICHES = os.environ.get(
    "POD_NICHES", "nursing,teacher,dogs,plants,fishing,pickleball,golf,gaming"
).split(",")

DISCOVER_SYSTEM = """You are the trend scout for a print-on-demand t-shirt store.
You receive RAW DATA: Google Trends related/rising queries and hot Reddit post
titles per niche. Propose t-shirt concepts that a specific identity group would
buy for THEMSELVES or as a gift, grounded ONLY in the data given.
Hard rules:
- No brand names, no celebrities, no song lyrics, no movie/TV/game quotes, no
  sports teams, no slogans you have seen on existing merchandise, no "Est. 19xx".
- The phrase must be original wording, max 6 words, printable in big block caps.
- Each concept must cite the exact data lines (verbatim) that support it.
Return JSON: a list of objects with keys niche, concept, phrase, subphrase,
keywords (3-6 search phrases people actually type), audience, style (one of
retro_sunset|line_art|bold_badge|minimal_mono|engraved|cute_kawaii), art_subject
(a text-free visual, e.g. 'a stethoscope curled into a heart'), palette,
layout (arch_top|type_top_art_bottom|stacked), evidence (list of verbatim lines)."""


def _raw_signals(niches: list[str]) -> dict:
    """Pull cheap public signals per niche. All free, all rate-limit polite."""
    from . import demand
    out: dict[str, dict] = {}
    for n in niches:
        n = n.strip()
        if not n:
            continue
        rec: dict = {"related": [], "reddit": []}
        try:
            rec["related"] = [
                f"{r.get('query')} ({r.get('value')})"
                for r in demand.google_trends_related_queries(f"{n} shirt")[:15]
            ]
        except Exception as exc:  # noqa: BLE001
            rec["related_error"] = str(exc)[:120]
        time.sleep(1.5)
        for sub in demand.NICHE_SUBREDDITS.get(n, [n])[:2]:
            try:
                rec["reddit"] += [p.get("title", "")[:140] for p in demand.reddit_hot(sub, limit=20)]
            except Exception as exc:  # noqa: BLE001
                rec.setdefault("reddit_error", []).append(str(exc)[:120])
            time.sleep(1.0)
        out[n] = rec
    return out


def cmd_discover(a: argparse.Namespace) -> int:
    from . import llm
    niches = [n for n in (a.niches or DEFAULT_NICHES) if n]
    raw = _raw_signals(niches)
    Path("state").mkdir(exist_ok=True)
    Path("state/raw_signals.json").write_text(json.dumps(raw, indent=1))
    prompt = (
        f"Propose up to {a.max} concepts total across these niches. RAW DATA:\n"
        + json.dumps(raw, indent=1)[:60000]
    )
    cands = llm.complete_json(prompt, DISCOVER_SYSTEM)
    if not isinstance(cands, list):
        log.error("discover: LLM did not return a list")
        return 1
    clean = []
    for c in cands:
        if not all(k in c for k in ("niche", "concept", "phrase", "keywords", "evidence")):
            continue
        if not c["evidence"]:
            continue  # ungrounded = discarded; demand.sweep re-verifies anyway
        clean.append(c)
    Path(a.out).write_text(json.dumps(clean, indent=1))
    log.info("discover: %d candidate(s) -> %s", len(clean), a.out)
    return 0


def cmd_design(a: argparse.Namespace) -> int:
    from .config import GUARDRAILS
    from .db import open_db
    from .pipeline import Pipeline
    from .sentinel import Sentinel

    cands = json.loads(Path(a.candidates).read_text())
    db = open_db()
    pipe, sent = Pipeline(db), Sentinel(db)
    results = []
    for c in cands:
        if db.listed_today() >= GUARDRAILS.max_new_listings_per_day:
            log.info("daily listing cap reached")
            break
        ok, why = sent.may_proceed()
        if not ok:
            log.error("sentinel refused: %s", why)
            break
        r = pipe.run_one(
            niche=c["niche"], concept=c["concept"], phrase=c["phrase"],
            keywords=list(c.get("keywords") or []), audience=c.get("audience", ""),
            style=c.get("style", "retro_sunset"), art_subject=c.get("art_subject", ""),
            subphrase=c.get("subphrase", ""), layout=c.get("layout", "arch_top"),
        )
        results.append({"concept": c["concept"], **r})
        log.info("%s -> %s", c["concept"], r.get("trace") or r.get("reason"))
    Path("state").mkdir(exist_ok=True)
    Path("state/last_design_run.json").write_text(json.dumps(results, indent=1, default=str))
    return 0


def cmd_ops(_: argparse.Namespace) -> int:
    from . import poll_orders, traffic
    from .db import open_db
    from .pipeline import Pipeline
    from .sentinel import Sentinel

    db = open_db()
    summary: dict = {"at": datetime.now(timezone.utc).isoformat()}
    summary["poll"] = poll_orders.run(db)
    summary["traffic"] = traffic.sync(db, Path("public/feed.xml"))
    summary["tests"] = Pipeline(db).evaluate_tests()
    verdict = Sentinel(db).evaluate()
    summary["sentinel"] = {"ok": bool(verdict), "detail": str(verdict)}
    try:
        from . import assets_github
        if os.environ.get("ASSETS_GH_REPO"):
            summary["asset_cleanup"] = assets_github.cleanup()
    except Exception as exc:  # noqa: BLE001
        summary["asset_cleanup_error"] = str(exc)[:200]
    Path("state").mkdir(exist_ok=True)
    Path("state/last_ops.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))
    return 0 if verdict else 2


def cmd_review(_: argparse.Namespace) -> int:
    from . import llm
    from .db import open_db
    from .sentinel import Sentinel

    db = open_db()
    report = Sentinel(db).daily_report()
    pnl = db.pnl_snapshot()
    with db.conn.cursor() as cur:
        cur.execute("SELECT * FROM v_kill_reasons LIMIT 15")
        kills = [dict(r) for r in cur.fetchall()]
    prompt = (
        "You are reviewing a fully autonomous print-on-demand store. Below are the "
        "guardrail report, P&L snapshot and kill reasons for the week. Write a short, "
        "blunt review for the owner: what is working, what is dying, which niches to "
        "drop or double, and ONE concrete parameter change (with the env var name). "
        "You cannot change anything yourself; the owner will.\n\n"
        f"GUARDRAILS:\n{report}\n\nPNL:\n{json.dumps(pnl, default=str)}\n\nKILLS:\n{json.dumps(kills, default=str)}"
    )
    try:
        text = llm.complete(prompt).text
    except llm.LLMError as exc:
        text = f"(LLM unavailable: {exc})\n\n{report}\n\n{json.dumps(pnl, indent=1, default=str)}"
    Path("REPORT.md").write_text(f"# Weekly review {datetime.now(timezone.utc):%Y-%m-%d}\n\n{text}\n")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    ap = argparse.ArgumentParser(prog="pod.batch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--niches", nargs="*")
    d.add_argument("--max", type=int, default=8)
    d.add_argument("--out", default="state/candidates.json")
    d.set_defaults(fn=cmd_discover)
    g = sub.add_parser("design")
    g.add_argument("--candidates", default="state/candidates.json")
    g.set_defaults(fn=cmd_design)
    sub.add_parser("ops").set_defaults(fn=cmd_ops)
    sub.add_parser("review").set_defaults(fn=cmd_review)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
