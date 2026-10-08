"""
Demand discovery and scoring.

The honest problem with "make designs based on popular demand": most automated
POD systems let an LLM *assert* that something is trending. It is confabulating.
It has no access to live data and it will produce a confident, plausible,
completely fabricated trend report.

So the rule in this module is structural, not prompt-based:

    AN IDEA IS NOT ALLOWED TO EXIST WITHOUT AT LEAST TWO MACHINE-CAPTURED
    SIGNALS FROM DIFFERENT SOURCES, EACH WITH A SAVED EVIDENCE URL.

Anything the LLM suggests gets demoted to a *hypothesis* and must then be
validated by a real lookup. The lookup either returns numbers or the idea dies.

Sources, in order of signal quality for POD:
  1. Google Trends            -- relative interest over time, breakout detection
  2. Etsy search result count -- competition density (fewer results + high
                                trend = the gap you want)
  3. Amazon Best Sellers      -- proven purchase intent in a category
  4. TikTok / IG hashtag      -- leading indicator, fastest but noisiest
  5. Reddit niche subs        -- audience language, the actual phrases people use
  6. Pinterest Trends         -- strong for seasonal/decor/hobby niches
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests

from .config import CREDS
from .state import Signal

log = logging.getLogger("pod.demand")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.37"
)


class DemandError(Exception):
    pass


# ---------------------------------------------------------------------------
# Source 1: Google Trends (unofficial widget API -- no key needed)
# ---------------------------------------------------------------------------

def google_trends_interest(
    keyword: str, geo: str = "US", timeframe: str = "today 3-m"
) -> dict[str, Any]:
    """
    Returns {"average": 0-100, "latest": 0-100, "delta": float, "breakout": bool}

    Uses the public trends widget endpoints. These are unofficial and can
    change without notice -- wrap every call site in try/except and treat a
    failure as "no signal", never as "zero demand". A source going dark must
    not silently make every idea look bad.
    """
    try:
        req = requests.get(
            "https://trends.google.com/trends/api/explore",
            params={
                "hl": "en-US",
                "tz": 420,
                "req": json.dumps(
                    {
                        "comparisonItem": [
                            {"keyword": keyword, "geo": geo, "time": timeframe}
                        ],
                        "category": 0,
                        "property": "",
                    }
                ),
            },
            headers={"User-Agent": UA},
            timeout=20,
        )
        # Response is prefixed with )]}' to defeat JSON hijacking.
        text = re.sub(r"^\)\]\}'", "", req.text).strip()
        data = json.loads(text)
        token = data["widgets"][0]["token"]
        req_meta = data["widgets"][0]["request"]

        series = requests.get(
            "https://trends.google.com/trends/api/widgetdata/multiline",
            params={
                "hl": "en-US",
                "tz": 420,
                "req": json.dumps(req_meta),
                "token": token,
            },
            headers={"User-Agent": UA},
            timeout=20,
        )
        stext = re.sub(r"^\)\]\}'", "", series.text).strip()
        sdata = json.loads(stext)
        points = [
            p["value"][0]
            for p in sdata["default"]["timelineData"]
            if p.get("value")
        ]
        if not points:
            return {"average": 0, "latest": 0, "delta": 0.0, "breakout": False, "error": "no points"}

        avg = sum(points) / len(points)
        recent = points[-4:] if len(points) >= 4 else points
        latest = sum(recent) / len(recent)
        early = points[: max(1, len(points) // 3)]
        baseline = sum(early) / len(early) or 1.0
        delta = (latest - baseline) / baseline
        return {
            "average": round(avg, 2),
            "latest": round(latest, 2),
            "delta": round(delta, 3),
            "breakout": latest >= 80 and delta >= 0.5,
            "n_points": len(points),
        }
    except Exception as exc:  # noqa: BLE001 -- a dead source is not a dead idea
        log.warning("google_trends_interest(%s) failed: %s", keyword, exc)
        return {"average": 0, "latest": 0, "delta": 0.0, "breakout": False, "error": str(exc)}


def google_trends_related_queries(keyword: str, geo: str = "US") -> list[dict]:
    """Rising/related queries -- this is where you find the *phrase* to print.
    'breakout' entries are the highest-value: a term that went from nothing to
    huge inside the window."""
    try:
        req = requests.get(
            "https://trends.google.com/trends/api/explore",
            params={
                "hl": "en-US",
                "tz": 420,
                "req": json.dumps(
                    {
                        "comparisonItem": [
                            {"keyword": keyword, "geo": geo, "time": "today 3-m"}
                        ],
                        "category": 0,
                        "property": "",
                    }
                ),
            },
            headers={"User-Agent": UA},
            timeout=20,
        )
        data = json.loads(re.sub(r"^\)\]\}'", "", req.text).strip())
        widget = next(
            (w for w in data["widgets"] if w["id"].endswith("RELATED_QUERIES")), None
        )
        if not widget:
            return []
        resp = requests.get(
            "https://trends.google.com/trends/api/widgetdata/relatedsearches",
            params={
                "hl": "en-US",
                "tz": 420,
                "req": json.dumps(widget["request"]),
                "token": widget["token"],
            },
            headers={"User-Agent": UA},
            timeout=20,
        )
        sdata = json.loads(re.sub(r"^\)\]\}'", "", resp.text).strip())
        rising = sdata["default"]["rankedList"][0]["rankedKeyword"]
        return [
            {
                "query": r["query"],
                "value": r.get("value", 0),
                "formatted": r.get("formattedValue", ""),
                "has_image": r.get("hasImage", False),
            }
            for r in rising
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("google_trends_related_queries(%s) failed: %s", keyword, exc)
        return []


# ---------------------------------------------------------------------------
# Source 2: Etsy competition density
# ---------------------------------------------------------------------------

def etsy_result_count(keyword: str) -> dict[str, Any]:
    """
    Count of listings matching a keyword. This is your COMPETITION measure, and
    it is the one most POD automation skips -- which is why most of it fails.

    High demand + high competition = you are the 40,000th seller of the same
    shirt. High demand + LOW competition = an actual gap.

    NOTE: scraping Etsy's search page violates their ToS and they block
    aggressively. The sanctioned path is the Etsy Open API v3 (requires an
    approved key) -- use that in production. This function exists as the shape
    of the check; wire it to the real API or to a browser step in OpenBot where
    the request comes from a real logged-in-ish Chromium profile.
    """
    official = _etsy_official_count(keyword)
    if official is not None:
        return official
    return {"count": None, "error": "Etsy v3 API key not configured", "source": "none"}


def _etsy_official_count(keyword: str) -> dict | None:
    key = getattr(CREDS, "etsy_key", "") or __import__("os").environ.get("ETSY_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            "https://openapi.etsy.com/v3/application/listings/active",
            params={"keywords": keyword, "limit": 1},
            headers={"x-api-key": key},
            timeout=20,
        )
        if r.status_code >= 400:
            return {"count": None, "error": r.text[:200], "source": "etsy_v3"}
        return {
            "count": r.json().get("count"),
            "source": "etsy_v3",
            "url": f"https://www.etsy.com/search?q={quote_plus(keyword)}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"count": None, "error": str(exc), "source": "etsy_v3"}


# ---------------------------------------------------------------------------
# Source 3: Amazon Best Sellers rank scrape (browser step in OpenBot)
# ---------------------------------------------------------------------------

AMAZON_BSR_URLS = {
    "novelty_tees": "https://www.amazon.com/Best-Sellers-Clothing-Shoes-Jewelry-Men-Novelty-T-Shirts/zgbs/fashion/1045800",
    "womens_novelty": "https://www.amazon.com/Best-Sellers-Clothing-Shoes-Jewelry-Womens-Novelty-T-Shirts/zgbs/fashion/1040660",
    "mugs": "https://www.amazon.com/Best-Sellers-Kitchen-Dining-Coffee-Mugs/zgbs/kitchen/16213601",
    "posters": "https://www.amazon.com/Best-Sellers-Posters-Prints/zgbs/home-garden/1063306",
}


def amazon_bsr_snapshot(category_url: str) -> list[dict]:
    """
    Intended to be executed as an OpenBot BROWSER step, not a raw requests
    call -- Amazon blocks datacenter IPs instantly and serves captcha. The
    bot's own Chromium container with a warm profile gets through far more
    often, and OpenBot's policy layer lets you allow exactly this host and
    nothing else.

    Returns [{"rank": int, "title": str, "price": str, "asin": str}]
    """
    raise NotImplementedError(
        "Run this as an OpenBot browser action. See openbot/bots.yaml, "
        "TrendScout skill 'amazon_bsr'. Do not call Amazon from a bare "
        "requests session -- it will be blocked and the failure will look "
        "like 'no demand' rather than 'blocked'."
    )


# ---------------------------------------------------------------------------
# Source 4: Reddit (official JSON endpoints, no key needed, be polite)
# ---------------------------------------------------------------------------

def reddit_hot(subreddit: str, limit: int = 50) -> list[dict]:
    """Subreddit hot posts. Reddit's public .json endpoints work without OAuth
    if you send a descriptive User-Agent and stay under ~1 req/sec."""
    try:
        r = requests.get(
            f"https://www.reddit.com/r/{subreddit}/hot.json",
            params={"limit": limit, "raw_json": 1},
            headers={"User-Agent": "pod-autopilot:research:v1.0 (by /u/yourusername)"},
            timeout=20,
        )
        if r.status_code >= 400:
            log.warning("reddit %s -> %s", subreddit, r.status_code)
            return []
        children = r.json().get("data", {}).get("children", [])
        return [
            {
                "title": c["data"].get("title", ""),
                "score": c["data"].get("score", 0),
                "comments": c["data"].get("num_comments", 0),
                "url": "https://reddit.com" + c["data"].get("permalink", ""),
                "created_utc": c["data"].get("created_utc", 0),
            }
            for c in children
            if not c["data"].get("stickied")
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("reddit_hot(%s) failed: %s", subreddit, exc)
        return []


# Relevant subreddits for POD niches. Extend per niche.
NICHE_SUBREDDITS = {
    "nursing": ["nursing", "NurseHumor", "StudentNurse"],
    "fishing": ["Fishing_Gear", "bassfishing", "FlyFishing"],
    "pickleball": ["pickleball", "PickleballTournaments"],
    "gaming": ["gaming", "pcmasterrace", "cozygamers"],
    "plants": ["houseplants", "IndoorGarden", "succulents"],
    "dogs": ["dogpictures", "Dogtraining", "corgi"],
    "teacher": ["Teachers", "teachingresources", "TeacherHumor"],
    "golf": ["golf", "golfcircles", "DiscGolfValhalla"],
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class ScoredIdea:
    niche: str
    concept: str
    keywords: list[str]
    audience: str
    signals: list[Signal] = field(default_factory=list)
    score: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    verdict: str = ""


def _norm(x: float, lo: float, hi: float) -> float:
    """Map x from [lo, hi] onto [0, 1], clamped."""
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def score_idea(
    *,
    niche: str,
    concept: str,
    keywords: Iterable[str],
    audience: str = "",
    trends: dict | None = None,
    related: list[dict] | None = None,
    competition_count: int | None = None,
    bsr_ranks: list[int] | None = None,
    reddit_posts: list[dict] | None = None,
) -> ScoredIdea:
    """
    Demand Score =
        0.30 * trend_momentum      (is interest rising right now?)
      + 0.20 * trend_magnitude     (how big is the absolute interest?)
      + 0.20 * competition_gap     (few sellers relative to interest?)
      + 0.15 * purchase_evidence   (are people actually BUYING, not just searching?)
      + 0.15 * audience_depth      (is there a community that self-identifies?)

    Each component is normalized to [0,1]. Weights are a starting prior --
    after ~50 experiments you should fit them to your own observed outcomes.
    """
    signals: list[Signal] = []
    keywords = list(keywords)
    breakdown: dict[str, float] = {}

    # -- trend momentum & magnitude ---------------------------------------
    momentum = magnitude = 0.0
    if trends:
        momentum = _norm(trends.get("delta", 0.0), -0.2, 1.5)
        magnitude = _norm(trends.get("latest", 0.0), 0, 80)
        if trends.get("breakout"):
            momentum = max(momentum, 0.9)
        for kw in keywords:
            signals.append(
                Signal(
                    source="google_trends",
                    query=kw,
                    metric="delta",
                    value=trends.get("delta", 0.0),
                    evidence_url=f"https://trends.google.com/trends/explore?q={quote_plus(kw)}&geo=US",
                    raw=trends,
                )
            )
    breakdown["trend_momentum"] = momentum
    breakdown["trend_magnitude"] = magnitude

    # -- breakout related queries are worth their own signal ---------------
    if related:
        for rq in related[:10]:
            if rq.get("formattedValue") == "Breakout" or rq.get("value", 0) >= 5000:
                signals.append(
                    Signal(
                        source="google_trends",
                        query=rq["query"],
                        metric="breakout",
                        value=float(rq.get("value", 0)),
                        evidence_url=f"https://trends.google.com/trends/explore?q={quote_plus(rq['query'])}&geo=US",
                        raw=rq,
                    )
                )

    # -- competition gap ---------------------------------------------------
    # The shape we want: interest high, listing count low. Use a log scale --
    # competition spans 3 orders of magnitude.
    gap = 0.0
    if competition_count is not None and trends:
        interest = max(trends.get("latest", 1.0), 1.0)
        # 0 results is usually "you typed it wrong", not "blue ocean". Penalize
        # a zero count hard -- it is far more often a bad keyword than an
        # untouched market.
        if competition_count == 0:
            gap = 0.05
        else:
            density = interest / math.log10(competition_count + 10)
            gap = _norm(density, 0, 40)
        signals.append(
            Signal(
                source="etsy",
                query=concept,
                metric="result_count",
                value=float(competition_count),
                evidence_url=f"https://www.etsy.com/search?q={quote_plus(concept)}",
            )
        )
    breakdown["competition_gap"] = gap

    # -- purchase evidence -------------------------------------------------
    purchase = 0.0
    if bsr_ranks:
        # Best rank in the sampled category. Top 20 = strong, top 5000 = weak.
        best = min(bsr_ranks)
        purchase = _norm(-math.log10(best + 1), -4.0, -0.7)
        for r in bsr_ranks[:5]:
            signals.append(
                Signal(
                    source="amazon_bsr",
                    query=niche,
                    metric="bsr_rank",
                    value=float(r),
                    evidence_url=AMAZON_BSR_URLS.get(niche, ""),
                )
            )
    breakdown["purchase_evidence"] = purchase

    # -- audience depth ----------------------------------------------------
    depth = 0.0
    if reddit_posts:
        # Median engagement of the hot page. A sub where the median post gets
        # 3 upvotes is not an audience you can sell to.
        scores = sorted(p.get("score", 0) for p in reddit_posts)
        median = scores[len(scores) // 2] if scores else 0
        depth = _norm(median, 0, 500)
        top = max(reddit_posts, key=lambda p: p.get("score", 0), default=None)
        if top:
            signals.append(
                Signal(
                    source="reddit",
                    query=niche,
                    metric="post_score",
                    value=float(top.get("score", 0)),
                    evidence_url=top.get("url"),
                )
            )
    breakdown["audience_depth"] = depth

    score = (
        0.30 * momentum
        + 0.20 * magnitude
        + 0.20 * gap
        + 0.15 * purchase
        + 0.15 * depth
    )

    verdict = _verdict(score, breakdown, signals)
    return ScoredIdea(
        niche=niche,
        concept=concept,
        keywords=keywords,
        audience=audience,
        signals=signals,
        score=round(score, 4),
        breakdown={k: round(v, 3) for k, v in breakdown.items()},
        verdict=verdict,
    )


def _verdict(score: float, breakdown: dict, signals: list[Signal]) -> str:
    sources = {s.source for s in signals}
    if len(sources) < 2:
        return (
            f"REJECT: only {len(sources)} corroborating source(s). "
            "Uncorroborated demand is indistinguishable from hallucination."
        )
    if score >= 0.72:
        return "STRONG: build now, priority queue."
    if score >= 0.55:
        return "VIABLE: build, standard queue."
    weakest = min(breakdown, key=lambda k: breakdown[k])
    return f"REJECT: score {score:.2f}. Weakest dimension: {weakest}."


# ---------------------------------------------------------------------------
# Full sweep: run every source for a keyword and return a ScoredIdea
# ---------------------------------------------------------------------------

def sweep(
    niche: str,
    concept: str,
    keywords: list[str],
    audience: str = "",
    subs: list[str] | None = None,
) -> ScoredIdea:
    """
    Deterministic, no LLM. Call this from the TrendScout bot after the LLM has
    proposed candidate concepts -- the LLM proposes, this disposes.

    Budget: ~5 HTTP calls per keyword. With a 15-minute OpenBot routine floor
    and 20-routine cap, run one sweep per niche per day, not continuously.
    """
    primary = keywords[0] if keywords else concept
    trends = google_trends_interest(primary)
    time.sleep(1.0)  # be polite; trends rate-limits hard on bursts
    related = google_trends_related_queries(primary)
    comp = etsy_result_count(concept)
    subs = subs or NICHE_SUBREDDITS.get(niche, [])
    posts: list[dict] = []
    for s in subs[:3]:
        posts.extend(reddit_hot(s, limit=25))
        time.sleep(1.0)

    return score_idea(
        niche=niche,
        concept=concept,
        keywords=keywords,
        audience=audience,
        trends=trends,
        related=related,
        competition_count=comp.get("count"),
        bsr_ranks=None,  # filled by the browser step, see amazon_bsr_snapshot
        reddit_posts=posts,
    )
