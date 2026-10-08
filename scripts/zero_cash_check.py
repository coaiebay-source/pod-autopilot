#!/usr/bin/env python3
"""
Readiness gate for the ZERO-CASH deployment. Run locally and as the first
step of every Actions run. Exit 1 = do not proceed.

It checks the free-tier wiring specifically, and it enforces the one rule of
this mode: NO PAID API KEYS. If OPENAI_API_KEY or ANTHROPIC_API_KEY are set
it refuses, because a mis-set LLM_PROVIDER/ART_PROVIDER could otherwise turn
a $0 plan into a bill while you sleep.
"""
from __future__ import annotations

import os
import shutil
import sys

OK, WARN, FAIL = "  ok  ", " WARN ", " FAIL "
problems = 0
warnings = 0


def row(status: str, msg: str) -> None:
    global problems, warnings
    if status == FAIL:
        problems += 1
    if status == WARN:
        warnings += 1
    print(f"[{status}] {msg}")


dry = os.environ.get("DRY_RUN", "1") not in ("0", "false", "False", "")
print(f"mode: {'DRY_RUN' if dry else 'LIVE'} | TRAFFIC_MODE={os.environ.get('TRAFFIC_MODE','paid')} "
      f"| ART_PROVIDER={os.environ.get('ART_PROVIDER','svg')} | LLM_PROVIDER={os.environ.get('LLM_PROVIDER','claude_code,gemini,github_models')}")

# 0. money leaks
for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    if os.environ.get(k):
        row(FAIL, f"{k} is set. Zero-cash mode forbids pay-per-token keys. Unset it.")
if float(os.environ.get("CAP_WEEKLY_ADS", "0") or 0) > 0 or float(os.environ.get("CAP_DAILY_ADS", "0") or 0) > 0:
    row(FAIL, "CAP_WEEKLY_ADS/CAP_DAILY_ADS must be 0 in zero-cash mode")
else:
    row(OK, "ad caps are 0 (no paid traffic)")
if os.environ.get("TRAFFIC_MODE", "paid") != "organic":
    row(FAIL, "TRAFFIC_MODE must be 'organic' (no impressions exist without ads)")
else:
    row(OK, "organic test gate active")

# 1. LLM lanes
have_llm = False
if shutil.which("claude"):
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.path.exists(os.path.expanduser("~/.claude")):
        row(OK, "claude_code lane: CLI present + subscription auth"); have_llm = True
    else:
        row(WARN, "claude CLI present but no CLAUDE_CODE_OAUTH_TOKEN / login (run `claude setup-token`)")
else:
    row(WARN, "claude CLI not installed (npm i -g @anthropic-ai/claude-code)")
if os.environ.get("GEMINI_API_KEY"):
    row(OK, "gemini lane: key set (AI Studio free tier)"); have_llm = True
else:
    row(WARN, "GEMINI_API_KEY unset -- recommended free fallback (aistudio.google.com)")
if os.environ.get("GH_MODELS_TOKEN"):
    row(OK, "github_models lane: token set"); have_llm = True
if not have_llm:
    row(FAIL, "no LLM lane configured; discover/design cannot run")

# 2. art lane
art = os.environ.get("ART_PROVIDER", "svg")
if art == "svg":
    try:
        import cairosvg  # noqa: F401
        row(OK, "svg lane: cairosvg importable")
    except ImportError:
        row(FAIL, "ART_PROVIDER=svg but cairosvg missing (pip install cairosvg; needs libcairo2)")
elif art == "cloudflare":
    if os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"):
        row(OK, "cloudflare lane: Workers AI creds set (free 10k neurons/day)")
    else:
        row(FAIL, "ART_PROVIDER=cloudflare but CF_ACCOUNT_ID/CF_API_TOKEN unset")
    if not os.environ.get("UPSCALER_CMD"):
        row(WARN, "cloudflare lane outputs 1024px; set UPSCALER_CMD (Real-ESRGAN) or prints are soft")
elif art == "openai":
    row(FAIL, "ART_PROVIDER=openai is pay-per-image; not allowed in zero-cash mode")

# 3. fonts
fd = os.environ.get("FONT_DIR", "./fonts")
if os.path.isdir(fd) and any(f.lower().endswith((".ttf", ".otf")) for f in os.listdir(fd)):
    row(OK, f"licensed font(s) present in {fd}")
else:
    row(FAIL, f"no .ttf/.otf in {fd} (Anton-Regular.ttf ships in the repo)")

# 4. storage + hosting + commerce (only strict when LIVE)
need = {
    "DATABASE_URL": "Neon/Supabase free Postgres (or POD_MEMORY_DB=1 for a dry demo)",
    "PRINTFUL_API_TOKEN": "Printful -> Settings -> API",
    "SQUARE_ACCESS_TOKEN": "developer.squareup.com app -> Production token",
    "SQUARE_LOCATION_ID": "Square Dashboard -> Locations",
    "APIFY_TOKEN": "Apify free plan ($5/mo credits) -- IP gate fails closed without it",
    "ASSETS_GH_REPO": "public repo for transient print files, e.g. you/pod-assets",
    "ASSETS_GH_TOKEN": "fine-grained PAT, Contents: read/write on that repo",
}
for k, hint in need.items():
    if os.environ.get(k) or (k == "DATABASE_URL" and os.environ.get("POD_MEMORY_DB")):
        row(OK, f"{k} set")
    else:
        row(FAIL if not dry else WARN, f"{k} unset -- {hint}")

for k, hint in {
    "CLICK_WORKER_URL": "deploy worker/click-counter (Cloudflare free) to get click counts",
    "CLICK_STATS_TOKEN": "wrangler secret put STATS_TOKEN",
    "FEED_SITE_URL": "GitHub Pages URL serving public/feed.xml for Pinterest auto-publish",
    "SQUARE_STORE_URL": "https://<store>.square.site",
}.items():
    row(OK if os.environ.get(k) else WARN, f"{k} {'set' if os.environ.get(k) else 'unset -- ' + hint}")

print()
print("MANUAL (one-time, all free) -- confirm before DRY_RUN=0:")
for line in (
    "Square Dashboard -> Online -> Items -> Item Sync -> visibility default = Visible",
    "Square Dashboard -> Online -> Settings -> Square Sync -> 'Mark newly imported items as unavailable online' = OFF",
    "Printful -> Settings -> Orders -> 'Manually confirm imported orders' = OFF",
    "Printful -> Billing -> a payment method exists (fulfillment is charged per real order, from the sale)",
    "Pinterest Business -> Settings -> Claimed accounts -> claim your GitHub Pages site -> Bulk create -> Auto-publish from RSS (feed.xml)",
    "GitHub repo -> Settings -> Pages -> Source: GitHub Actions",
    "GitHub repo -> Settings -> Variables: DRY_RUN=0 is the only switch between rehearsal and live",
):
    print(f"   [ ] {line}")
print()
print(f"{problems} problem(s), {warnings} warning(s)")
sys.exit(1 if problems else 0)
