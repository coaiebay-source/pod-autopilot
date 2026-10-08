"""
IP clearance. FAIL CLOSED.

This is the module that keeps an autonomous pipeline from destroying your
business. A hand-designed POD shop ships maybe 5 designs a week and the human
eyeballs each one. An automated shop ships 40. The failure rate on IP is not
zero, so at volume you WILL hit a trademarked phrase or a protected character
unless something structural stops you.

Consequences are asymmetric and severe: Square can terminate the account,
Printful can ban the store, and rights holders do send demand letters. Losing
$8 of margin on a shirt is a bad day; losing the merchant account is the end
of the business. So this gate rejects on uncertainty.

Layers, cheapest first (short-circuit on the first hit):
  1. Local blocklist      -- free, instant, catches the obvious 90%
  2. Pattern rules        -- celebrity names, team names, franchise words
  3. USPTO TESS Class 25  -- authoritative for apparel. Via Apify actor
                             (~$5 per 1,000 text checks) or direct TESS.
  4. Reverse image search -- catches stolen ART, which text checks miss
                             entirely. Most POD IP theft is artwork, not words.
  5. LLM judgment         -- last, and only as a tiebreaker. Never as the
                             primary check: an LLM will happily tell you a
                             phrase is "probably fine."

Nice Class 25 = clothing/footwear/headgear. Also check 24 (textiles), 21 (mugs),
16 (paper goods/posters), 18 (bags/totes) depending on the product.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import CREDS

log = logging.getLogger("pod.trademark")

NICE_CLASS_BY_PRODUCT = {
    "tshirt": [25],
    "hoodie": [25],
    "mug": [21, 24],
    "poster": [16],
    "tote": [18, 25],
    "phone_case": [9],
    "hat": [25],
}


# ---------------------------------------------------------------------------
# Layer 1: local blocklist
# ---------------------------------------------------------------------------
# Seed list. Grow this from every rejection and every takedown you receive --
# it becomes the most valuable file in the system over time.
HARD_BLOCK_TERMS: set[str] = {
    # Franchises / studios
    "disney", "mickey mouse", "minnie", "marvel", "spiderman", "spider-man",
    "batman", "superman", "dc comics", "star wars", "baby yoda", "grogu",
    "mandalorian", "harry potter", "hogwarts", "pokemon", "pokémon", "pikachu",
    "nintendo", "mario", "zelda", "sonic", "hello kitty", "sanrio",
    "spongebob", "peppa pig", "bluey", "paw patrol", "frozen", "elsa",
    "taylor swift", "swiftie", "beyonce", "beyoncé", "drake", "kanye", "ye",
    "travis kelce", "eras tour", "barbie", "oppenheimer", "wednesday addams",
    # Sports -- leagues and teams are the single most litigious category
    "nfl", "nba", "mlb", "nhl", "mls", "nascar", "ufc", "wwe", "olympics",
    "olympic", "dallas cowboys", "cowboys", "lakers", "celtics", "yankees",
    "warriors", "chiefs", "eagles", "packers", "steelers", "alabama crimson tide",
    "crimson tide", "ohio state", "texas longhorns", "longhorns",
    # Brands
    "nike", "adidas", "gucci", "louis vuitton", "chanel", "rolex", "yeti",
    "stanley cup", "stanley tumbler", "coca cola", "coca-cola", "pepsi",
    "starbucks", "apple", "iphone", "playstation", "xbox", "lego",
    "crocs", "lululemon", "the north face", "patagonia", "carhartt",
    # Common-phrase traps. These FEEL generic and are registered marks.
    "let's go brandon", "girl boss", "boss babe", "mom life",  # mom life: many live marks
    "it's giving", "main character energy",
    "world's best dad", "best dad ever",  # multiple live registrations
    "mama bear",  # heavily registered in Class 25
    "dog mom",  # registered variants exist -- screen, don't assume
    "boo", "hocus pocus", "basic witch",  # seasonal marks spike every year
    "jesus is king",  # registered
    "make america great again", "maga", "build back better",
    # Profanity-adjacent that trips platform policies even when not trademarked
    "fuck", "shit", "bitch", "asshole",
}

# Words that make a phrase risky even in combination. Screen any phrase
# containing these against TESS rather than auto-rejecting.
SCREEN_CAREFULLY: set[str] = {
    "mama", "mom", "dad", "papa", "grandma", "grandpa", "nana", "tito",
    "nurse", "teacher", "coach", "engineer", "accountant",
    "boss", "queen", "king", "legend", "vibes", "squad", "gang", "crew",
    "est", "since", "original", "official", "authentic",
}

# Structural patterns that indicate somebody else's brand.
PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:est\.|est|since)\s*(?:19|20)\d{2}\b", "established-date mark (commonly registered)"),
    (r"\b[A-Z]{2,}\s*®", "registered-mark symbol present"),
    (r"\b[A-Z]{2,}\s*™", "trademark symbol present"),
    (r"\b(?:fc|cf|ac|sc)\s+[a-z]+\b", "possible football/soccer club name"),
    (r"\b(?:university|college|state)\s+of\s+[a-z]+\b", "possible school mark"),
    (r"\blyrics?\b|\bquote[d]?\b", "possible song lyric / quotation -- copyright risk"),
    # "in my <x> era" is a template, so it needs a pattern not a literal.
    # Heavily litigated space; several variants are live marks in Class 25.
    (r"\bin my \w+ era\b", "'in my _ era' template -- multiple live Class 25 marks"),
    (r"\b(?:girl|boss|babe)\s*(?:boss|babe|girl)\b", "girl boss / boss babe family -- registered"),
]


@dataclass
class Match:
    term: str
    layer: str
    severity: str          # "HIGH" | "MEDIUM" | "LOW"
    detail: str = ""
    url: str | None = None


@dataclass
class ScreenResult:
    risk: str              # "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN"
    completed: bool = False
    error: str | None = None
    matches: list[Match] = field(default_factory=list)
    layers_run: list[str] = field(default_factory=list)
    checked_text: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "completed": self.completed,
            "error": self.error,
            "matches": [m.__dict__ for m in self.matches],
            "layers_run": self.layers_run,
            "checked_text": self.checked_text,
        }


def CREDS_is_dry_run() -> bool:
    from .config import DRY_RUN
    return DRY_RUN


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def layer_blocklist(texts: list[str]) -> list[Match]:
    hits: list[Match] = []
    for t in texts:
        low = t.lower()
        toks = _tokens(t)
        for term in HARD_BLOCK_TERMS:
            if " " in term:
                if term.lower() in low:
                    hits.append(Match(term, "blocklist", "HIGH", f"phrase present in {t!r}"))
            elif term in toks:
                hits.append(Match(term, "blocklist", "HIGH", f"token present in {t!r}"))
        for pat, why in PATTERNS:
            if re.search(pat, t, re.IGNORECASE):
                hits.append(Match(pat, "pattern", "MEDIUM", why))
    return hits


# ---------------------------------------------------------------------------
# Layer 3: USPTO TESS via Apify actor
# ---------------------------------------------------------------------------

APIFY_TESS_ACTOR = os.environ.get(
    "APIFY_TESS_ACTOR", "dev00/uspto-trademark-text-check-api"
)


def layer_uspto(
    texts: list[str], nice_classes: list[int], status: str = "live"
) -> tuple[list[Match], str | None]:
    """
    Query USPTO for live marks in the relevant Nice classes.

    Apify actor route is ~$5/1,000 checks and handles TESS's anti-bot
    behavior for you. At 6 designs/day * 3 text fields = 18 checks/day =
    ~$2.70/month. That is the cheapest insurance in this entire system.

    Returns (matches, error). On error the caller MUST fail closed.
    """
    if not CREDS.apify_token:
        return [], "APIFY_TOKEN not set -- cannot run authoritative USPTO check"
    matches: list[Match] = []
    try:
        for text in texts:
            for nice in nice_classes:
                run = requests.post(
                    f"https://api.apify.com/v2/acts/{APIFY_TESS_ACTOR}/run-sync-get-dataset-items",
                    params={"token": CREDS.apify_token},
                    json={
                        "searchText": text,
                        "internationalClass": f"{nice:03d}",
                        "statusType": status,
                        "resultsPage": 1,
                    },
                    timeout=180,
                )
                if run.status_code >= 400:
                    return [], f"Apify {run.status_code}: {run.text[:200]}"
                items = run.json() if run.text.strip().startswith(("[", "{")) else []
                for it in items if isinstance(items, list) else []:
                    mark = str(it.get("mark_text") or it.get("wordmark") or "").strip()
                    if not mark:
                        continue
                    sev = "HIGH" if _is_live(it) else "MEDIUM"
                    matches.append(
                        Match(
                            term=mark,
                            layer=f"uspto_class_{nice}",
                            severity=sev,
                            detail=(
                                f"serial={it.get('serial_number')} "
                                f"status={it.get('status_description') or it.get('status_code')} "
                                f"owner={it.get('owner', {}).get('name') if isinstance(it.get('owner'), dict) else it.get('owner')} "
                                f"for text {text!r}"
                            ),
                            url=f"https://tsdr.uspto.gov/#caseNumber={it.get('serial_number')}&caseType=SERIAL_NO",
                        )
                    )
        return matches, None
    except Exception as exc:  # noqa: BLE001
        return [], f"USPTO check exception: {exc}"


def _is_live(item: dict) -> bool:
    s = str(item.get("status_description") or item.get("status_code") or "").lower()
    live = item.get("live_dead") or item.get("liveDead")
    if live:
        return str(live).upper() == "LIVE"
    return "live" in s or "registered" in s or "published" in s or "pending" in s


# ---------------------------------------------------------------------------
# Layer 4: reverse image search for stolen artwork
# ---------------------------------------------------------------------------

def layer_reverse_image(image_url: str) -> tuple[list[Match], str | None]:
    """
    Text checks cannot catch a bot that regenerated an existing artist's style
    or, worse, passed through scraped art. Reverse-image the finished design.

    Implementations, best to worst for automation:
      * Google Lens via an OpenBot BROWSER step (real Chromium, human-shaped
        interaction, survives where API wrappers die)
      * TinEye API (paid, structured, best for commercial clearance)
      * Bing Visual Search API

    The gate treats "could not check" as a failure. If you cannot verify the
    art is original, you do not ship it.
    """
    if not image_url:
        return [], "no image to check"
    # Placeholder: wire to TinEye or drive via OpenBot browser skill.
    return [], "reverse image search not wired -- FAIL CLOSED until implemented"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def screen(
    *,
    concept: str,
    title: str,
    description: str,
    design_text: list[str],
    product_type: str = "tshirt",
    image_url: str | None = None,
    run_uspto: bool = True,
    run_reverse: bool = True,
    demo_skip_uspto: bool = False,
) -> ScreenResult:
    """
    Screen every piece of text that will be published or printed. Note that
    design_text (the words ON the shirt) is screened separately from title and
    description -- a phrase can be unregistered in Class 25 but still get you
    a takedown if it is a song lyric or a celebrity catchphrase.
    """
    res = ScreenResult(risk="UNKNOWN")
    nice = NICE_CLASS_BY_PRODUCT.get(product_type, [25])
    texts = [t for t in [concept, title, description, *design_text] if t]
    res.checked_text = texts

    try:
        # Layer 1 + 2
        res.layers_run.append("blocklist")
        hits = layer_blocklist(texts)
        res.matches.extend(hits)

        # Layer 3
        if demo_skip_uspto:
            # DEMO ONLY. Refuses to run outside DRY_RUN. Skipping the
            # authoritative registry in production is exactly the failure this
            # module exists to prevent, so the switch is physically incapable
            # of being left on by accident.
            if not CREDS_is_dry_run():
                raise RuntimeError(
                    "demo_skip_uspto requested outside DRY_RUN -- refusing. "
                    "Publishing without the USPTO layer is how accounts die."
                )
            log.critical(
                "USPTO LAYER SKIPPED BY DEMO FLAG. This screen is NOT a real "
                "clearance. Blocklist only."
            )
            res.layers_run.append("uspto:DEMO_SKIPPED")
        elif run_uspto:
            res.layers_run.append("uspto")
            uspto_matches, err = layer_uspto(texts, nice)
            if err:
                # FAIL CLOSED. Do not publish because the checker was down.
                res.error = err
                res.risk = "UNKNOWN"
                res.completed = False
                log.error("IP screen unavailable, failing closed: %s", err)
                return res
            res.matches.extend(uspto_matches)

        # Layer 4
        if run_reverse and image_url:
            res.layers_run.append("reverse_image")
            rev, rerr = layer_reverse_image(image_url)
            if rerr:
                res.error = rerr
                res.risk = "UNKNOWN"
                res.completed = False
                return res
            res.matches.extend(rev)

        res.completed = True
        high = [m for m in res.matches if m.severity == "HIGH"]
        med = [m for m in res.matches if m.severity == "MEDIUM"]
        if high:
            res.risk = "HIGH"
        elif med:
            res.risk = "MEDIUM"
        else:
            res.risk = "LOW"
        log.info(
            "IP screen risk=%s matches=%d layers=%s",
            res.risk, len(res.matches), res.layers_run,
        )
        return res

    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
        res.risk = "UNKNOWN"
        res.completed = False
        return res


def phrase_alternatives(rejected: str, why: str) -> str:
    """
    When a concept is rejected, the Design Director needs to know HOW to pivot,
    not just that it failed. Return guidance text for the next attempt.

    Deliberately returns instructions rather than generating alternatives
    itself -- alternative generation is the LLM's job, and it must be
    re-screened. Never let a pivot skip the screen.
    """
    return (
        f"Concept {rejected!r} was rejected: {why}. "
        "Generate a replacement that (a) avoids the flagged term entirely, not a "
        "misspelling or stylized variant of it -- evasive spelling is still "
        "infringement and looks worse in a dispute; (b) uses language drawn from "
        "the audience's own vocabulary captured in the reddit/trends signals; "
        "(c) is generic enough that no single entity could own it, but specific "
        "enough that the niche recognizes itself. Then submit for re-screening."
    )
