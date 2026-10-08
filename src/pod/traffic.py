"""
$0 traffic: get strangers to look at the listings without buying ads.

Channels, in order of autonomy and payoff for POD:

  1. PINTEREST AUTO-PUBLISH FROM RSS  (the workhorse)
     Pinterest Business accounts can auto-create Pins from an RSS feed on a
     claimed website (Settings -> Bulk create Pins -> Auto-publish). We host
     feed.xml on GitHub Pages (free) with a mockup image + tracked link per
     listing. Pinterest polls it ~daily and pins everything new. Pinterest is
     search-driven and evergreen: pins keep surfacing for months, which is
     exactly the slow-burn organic test this mode runs.

  2. CLICK TRACKING
     Every outbound link is  https://<worker>/go/<exp_id>  (worker/click-counter).
     pull_clicks() reads the counts and writes them to experiments.clicks.

  3. REDDIT / TIKTOK / IG
     Deliberately NOT automated. Self-promo rules get accounts banned, and a
     ban on your personal account is a real cost. If you want them, that's a
     5-minute-a-day human job, not a bot job.

  4. SQUARE ONLINE SEO
     Free. Titles/descriptions are template-built with the keyword list, and
     Square auto-generates the sitemap. Slow but nonzero.

Metrics note: GitHub Pages has no logs and Pinterest Analytics needs an
approved API app, so impressions are unknown. The organic test gate is
therefore clicks+orders over time (see config.Guardrails organic_*).
"""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from .db import DB
from .state import Stage

log = logging.getLogger("pod.traffic")


def worker_base() -> str:
    return os.environ.get("CLICK_WORKER_URL", "").rstrip("/")


def tracked_link(exp_id: str, fallback: str) -> str:
    base = worker_base()
    return f"{base}/go/{exp_id}" if base else fallback


# ---------------------------------------------------------------------------
# Click counter sync
# ---------------------------------------------------------------------------

def register_links(db: DB) -> int:
    """Tell the Worker where each live experiment's product page is."""
    base, tok = worker_base(), os.environ.get("CLICK_STATS_TOKEN", "")
    if not (base and tok):
        log.warning("CLICK_WORKER_URL / CLICK_STATS_TOKEN not set; links not registered")
        return 0
    links = {}
    for exp in db.by_stage(Stage.TESTING) + db.by_stage(Stage.SCALING):
        url = _product_url(exp)
        if url:
            links[exp.id] = url
    if not links:
        return 0
    r = requests.put(f"{base}/links", json=links,
                     headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    r.raise_for_status()
    return len(links)


def pull_clicks(db: DB, days: int = 60) -> dict[str, int]:
    """Read cumulative clicks per experiment and SET (not add) the counter."""
    base, tok = worker_base(), os.environ.get("CLICK_STATS_TOKEN", "")
    if not (base and tok):
        return {}
    r = requests.get(f"{base}/stats", params={"days": days},
                     headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    r.raise_for_status()
    stats: dict[str, int] = r.json()
    with db.conn.cursor() as cur:
        for exp_id, n in stats.items():
            cur.execute(
                "UPDATE experiments SET clicks=%s, updated_at=%s WHERE id=%s",
                (int(n), datetime.now(timezone.utc), exp_id),
            )
    db.conn.commit()
    log.info("clicks synced for %d experiment(s)", len(stats))
    return stats


# ---------------------------------------------------------------------------
# Pinterest RSS feed
# ---------------------------------------------------------------------------

def _product_url(exp) -> str:
    for h in reversed(exp.history):
        if h.get("event") == "published" and h.get("storefront_url"):
            return h["storefront_url"]
    store = os.environ.get("SQUARE_STORE_URL", "").rstrip("/")
    if store and exp.square_item_id:
        # Square Online product slugs are not exposed by any API, so we link to
        # the site's own search page for the exact title. It resolves to one
        # result and is stable. (yourstore.square.site/s/search?q=...)
        from urllib.parse import quote_plus
        title = next((h.get("title") for h in reversed(exp.history) if h.get("title")), exp.concept)
        return f"{store}/s/search?q={quote_plus(title)}"
    return ""


def write_pinterest_feed(db: DB, out_path: Path, site_url: str, max_items: int = 100) -> int:
    """RSS 2.0 with media enclosures: one item per live listing. Pinterest
    needs <title>, <link>, <description>, and an image via <enclosure> or an
    <img> in the description. Newest first."""
    items = []
    exps = db.by_stage(Stage.TESTING) + db.by_stage(Stage.SCALING)
    exps.sort(key=lambda e: e.listed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for exp in exps[:max_items]:
        img = exp.mockup_urls[0] if exp.mockup_urls else None
        page = _product_url(exp)
        if not (img and page):
            continue
        link = tracked_link(exp.id, page)
        title = html.escape(f"{exp.concept} T-Shirt")
        desc = html.escape(
            f"{exp.concept}. Soft unisex tee, printed to order. "
            f"{' '.join('#' + k.replace(' ', '') for k in exp.keywords[:4])}"
        )
        pub = (exp.listed_at or datetime.now(timezone.utc)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        items.append(
            f"<item><title>{title}</title><link>{html.escape(link)}</link>"
            f"<guid isPermaLink=\"false\">{exp.id}</guid><pubDate>{pub}</pubDate>"
            f"<description>{desc}</description>"
            f"<enclosure url=\"{html.escape(img)}\" type=\"image/jpeg\" length=\"0\"/></item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
        f"<title>{html.escape(os.environ.get('STORE_NAME', 'New designs'))}</title>"
        f"<link>{html.escape(site_url)}</link><description>Fresh tees, printed to order.</description>"
        + "".join(items) + "</channel></rss>\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(xml)
    log.info("pinterest feed: %d item(s) -> %s", len(items), out_path)
    return len(items)


def sync(db: DB, feed_path: Path | None = None) -> dict:
    """Called from the ops run."""
    out = {"links": 0, "clicks": 0, "feed_items": 0}
    try:
        out["links"] = register_links(db)
        out["clicks"] = len(pull_clicks(db))
    except requests.RequestException as exc:
        log.error("click worker sync failed: %s", exc)
        db.log_event("poll_error", {"src": "click_worker", "err": str(exc)})
    site = os.environ.get("FEED_SITE_URL", "")
    if site:
        out["feed_items"] = write_pinterest_feed(db, feed_path or Path("public/feed.xml"), site)
    return out
