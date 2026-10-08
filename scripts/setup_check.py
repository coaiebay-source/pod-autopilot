#!/usr/bin/env python3
"""
Readiness checker. Answers "am I allowed to turn this on yet?" in one command.

    python scripts/setup_check.py            # dry-run readiness
    LIVE=1 python scripts/setup_check.py     # production readiness (stricter)

Exits 0 only when there are no FAILs. WARNs are judgment calls. MANUAL items
are dashboard clicks no API can verify -- the checker prints them as a list you
tick off by eye, because those four settings are the ones that silently stop
fulfillment when wrong.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "src"))

LIVE = os.environ.get("LIVE", "") not in ("", "0", "false")

PASS, WARN, FAIL, INFO, MANUAL = [], [], [], [], []


def ok(label, detail=""):
    PASS.append((label, detail))


def warn(label, detail=""):
    WARN.append((label, detail))


def fail(label, detail=""):
    FAIL.append((label, detail))


def info(label, detail=""):
    INFO.append((label, detail))


def manual(label, detail=""):
    MANUAL.append((label, detail))


print("POD AUTOPILOT READINESS CHECK", "(LIVE mode)" if LIVE else "(dry-run mode)")
print("=" * 72)

# ---------------------------------------------------------------------------
# 1. Runtime
# ---------------------------------------------------------------------------
v = sys.version_info
if (v.major, v.minor) >= (3, 11):
    ok(f"python {v.major}.{v.minor}.{v.micro}")
else:
    fail(f"python {v.major}.{v.minor}", "need >= 3.11")

for mod, why in [
    ("PIL", "design compositing"),
    ("requests", "all API clients"),
    ("psycopg", "Postgres store"),
    ("fastapi", "webhook receiver"),
    ("uvicorn", "webhook receiver"),
    ("mcp", "OpenBot tool server"),
]:
    try:
        importlib.import_module(mod)
        ok(f"import {mod}", why)
    except ImportError:
        (fail if mod in ("PIL", "requests", "psycopg") else warn)(
            f"import {mod}", f"missing -- needed for {why}. pip install -e '.[dev]'"
        )

# ---------------------------------------------------------------------------
# 2. Fonts
# ---------------------------------------------------------------------------
fonts = sorted((HERE / "fonts").glob("*.ttf")) + sorted((HERE / "fonts").glob("*.otf"))
if fonts:
    ok(f"fonts: {len(fonts)} face(s)", ", ".join(f.name for f in fonts[:4]))
    manual(
        "font license",
        "confirm every face in fonts/ permits COMMERCIAL EMBEDDING in merchandise "
        "(OFL faces like Anton/Bebas Neue do; desktop-licensed retail fonts do not)",
    )
else:
    fail("fonts", "no .ttf/.otf in fonts/ -- typography falls back to a bitmap font "
                  "that looks broken at 300 DPI. Download Anton (OFL).")

# ---------------------------------------------------------------------------
# 3. Upscaler (quality, not safety)
# ---------------------------------------------------------------------------
if os.environ.get("UPSCALER_CMD"):
    ok("UPSCALER_CMD set", os.environ["UPSCALER_CMD"][:60])
else:
    warn(
        "UPSCALER_CMD unset",
        "generated art (1024px) will be upscaled ~3.2x onto the print canvas = "
        "~95 effective DPI, below Printful's 150 DPI apparel floor. Install "
        "realesrgan-ncnn-vulkan and set UPSCALER_CMD (see docs/RUNBOOK.md part 3).",
    )

# ---------------------------------------------------------------------------
# 4. Environment / credentials
# ---------------------------------------------------------------------------
def env_state(name, required_live, required_dry=False, secret=True):
    val = os.environ.get(name, "")
    if val:
        ok(f"{name} set", ("(secret)" if secret else val[:40]))
        return val
    if (LIVE and required_live) or required_dry:
        fail(f"{name} missing", "required" + (" in live mode" if not required_dry else ""))
    else:
        warn(f"{name} missing", "optional in dry-run, required live")
    return ""


asset_url = env_state("ASSET_BASE_URL", True, required_dry=True, secret=False)
pft = env_state("PRINTFUL_API_TOKEN", True)
sq = env_state("SQUARE_ACCESS_TOKEN", True)
env_state("SQUARE_LOCATION_ID", False)
env_state("SQUARE_WEBHOOK_SIGNATURE_KEY", True)
env_state("OPENAI_API_KEY", True)
prov = os.environ.get("BOT_PROVIDER", "anthropic")
env_state({"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "google": "GOOGLE_API_KEY"}.get(prov, "ANTHROPIC_API_KEY"), True)
apify = env_state("APIFY_TOKEN", True)
if LIVE and not apify:
    FAIL[-1] = (
        "APIFY_TOKEN missing",
        "WITHOUT IT THE IP LAYER CANNOT RUN, AND THE IP GATE FAILS CLOSED -- the "
        "pipeline will screen, get UNKNOWN, and kill every design. Nothing will "
        "ever publish. This is the safety system working; fund the $5/mo actor.",
    )
env_state("ETSY_API_KEY", False)
env_state("PUBLIC_WEBHOOK_URL", True)
env_state("PRINTFUL_HOOK_SECRET", True)
env_state("DATABASE_URL", True, secret=False)

if asset_url and not asset_url.startswith("https://"):
    fail("ASSET_BASE_URL not https", "Printful fetches print files over TLS")

pwu = os.environ.get("PUBLIC_WEBHOOK_URL", "")
if pwu and not pwu.startswith("https://"):
    fail("PUBLIC_WEBHOOK_URL not https", "Square requires https notification URLs")

if os.environ.get("DRY_RUN", "1") in ("0", "false", "") and not LIVE:
    warn("DRY_RUN=0 but LIVE not set", "you are pointed at production without running "
                                       "the live checklist. Set LIVE=1 for this check.")

# ---------------------------------------------------------------------------
# 5. Live connectivity (only with real tokens)
# ---------------------------------------------------------------------------
if pft:
    import requests
    try:
        r = requests.get("https://api.printful.com/stores",
                         headers={"Authorization": f"Bearer {pft}"}, timeout=20)
        if r.status_code == 200:
            stores = r.json().get("result", [])
            ok("Printful token valid", f"{len(stores)} store(s)")
            if not any("square" in str(s).lower() for s in stores):
                warn("no Square-connected store visible",
                     "confirm Printful -> Stores shows your Square store as connected")
        elif r.status_code == 401:
            fail("Printful token rejected (401)", "regenerate in Printful -> Settings -> API")
        else:
            warn(f"Printful {r.status_code}", r.text[:120])
    except Exception as exc:  # noqa: BLE001
        warn("Printful unreachable", str(exc)[:120])

if sq:
    import requests
    base = ("https://connect.squareupsandbox.com"
            if os.environ.get("SQUARE_SANDBOX") else "https://connect.squareup.com")
    try:
        r = requests.get(f"{base}/v2/locations",
                         headers={"Authorization": f"Bearer {sq}",
                                  "Square-Version": "2026-09-16"}, timeout=20)
        if r.status_code == 200:
            locs = r.json().get("locations", [])
            ok("Square token valid", f"{len(locs)} location(s)")
            if not os.environ.get("SQUARE_LOCATION_ID") and locs:
                info("SQUARE_LOCATION_ID", f"probably {locs[0]['id']}")
        elif r.status_code == 401:
            fail("Square token rejected (401)",
                 "regenerate the Personal Access Token; check scopes include "
                 "ITEMS_WRITE ITEMS_READ ORDERS_READ PAYMENTS_READ")
        else:
            warn(f"Square {r.status_code}", r.text[:120])
    except Exception as exc:  # noqa: BLE001
        warn("Square unreachable", str(exc)[:120])

if asset_url:
    import requests
    try:
        r = requests.get(asset_url.rstrip("/") + "/", timeout=15, allow_redirects=True)
        if r.status_code < 500:
            ok("ASSET_BASE_URL reachable", f"HTTP {r.status_code} (403/404 on root is fine)")
        else:
            warn("ASSET_BASE_URL server error", str(r.status_code))
    except Exception as exc:  # noqa: BLE001
        (fail if LIVE else warn)("ASSET_BASE_URL unreachable",
                                 "Printful cannot fetch print files from here: " + str(exc)[:100])

# ---------------------------------------------------------------------------
# 6. Database
# ---------------------------------------------------------------------------
if LIVE or os.environ.get("POD_MEMORY_DB") != "1":
    try:
        import psycopg
        dsn = os.environ.get("DATABASE_URL", "postgresql://pod:pod@localhost:5433/pod")
        with psycopg.connect(dsn, connect_timeout=5) as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.experiments')")
                has = cur.fetchone()[0]
        if has:
            ok("Postgres reachable, schema present")
        else:
            fail("schema missing", "run: psql <db> -f sql/schema.sql")
    except Exception as exc:  # noqa: BLE001
        (fail if LIVE else warn)("Postgres unreachable", str(exc)[:140])

# ---------------------------------------------------------------------------
# 7. OpenBot host prerequisites
# ---------------------------------------------------------------------------
docker = shutil.which("docker")
bun = shutil.which("bun")
info("docker", docker or "NOT FOUND (OpenBot needs Docker)")
info("bun", bun or "NOT FOUND (needed for clone-based OpenBot; the published "
                   "Docker image does not need it)")
if not docker:
    warn("docker missing", "OpenBot deploys via Docker")

# ---------------------------------------------------------------------------
# 8. The manual checklist -- the four silent killers
# ---------------------------------------------------------------------------
manual("Square: item visibility default",
       "Dashboard -> Online -> Items -> Item Sync -> Item visibility settings = Visible")
manual("Square: auto-unimport off",
       "Dashboard -> Online -> Settings -> Square Sync -> 'Mark newly imported items "
       "as unavailable online' = OFF")
manual("Printful: billing method present",
       "Dashboard -> Billing -> Billing methods. Without a card, order import stops "
       "silently and nothing prints.")
manual("Printful: manual confirm off",
       "Stores -> <store> -> Edit -> Orders -> 'Manually confirm imported orders' = OFF. "
       "If ON, every order parks in NEEDS_APPROVAL: paid customers, no product.")
manual("OpenBot: policy replaced",
       "AGENT_COMPUTER_POLICY is openbot/policy.json, NOT the shipped allow-all default")
manual("OpenBot: not internet-exposed",
       "UI on localhost / Tailscale / VPN only. It is alpha software with a no-auth default.")
manual("OpenBot: per-Bot computers on",
       "COMPUTER_SUPERVISOR_URL set. TrendScout browses hostile pages; CatalogBot holds "
       "your Square session. They must not share a browser profile.")
manual("Kill switch reachable",
       "You can run `python -m pod.cli halt` from a phone (SSH app) or the halt is wired "
       "to a Slack/SMS command. An unreachable kill switch is not a kill switch.")

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print()
for label, detail in INFO:
    print(f"  info  {label}: {detail}")
for label, detail in PASS:
    print(f"  PASS  {label}  {detail}")
for label, detail in WARN:
    print(f"  WARN  {label}\n        {detail}")
for label, detail in FAIL:
    print(f"  FAIL  {label}\n        {detail}")

print()
print("  MANUAL CHECKLIST (tick by eye -- no API can verify these):")
for i, (label, detail) in enumerate(MANUAL, 1):
    print(f"   [ ] {i}. {label}\n           {detail}")

print()
print(f"  {len(PASS)} pass / {len(WARN)} warn / {len(FAIL)} fail / {len(MANUAL)} manual")
if FAIL:
    print("\n  NOT READY. Fix every FAIL first.\n")
    sys.exit(1)
if LIVE and WARN:
    print("\n  Ready-ish for live, with warnings above. Each WARN is a degraded "
          "safety or quality layer -- read them before proceeding.\n")
else:
    print("\n  Ready for the next step in docs/RUNBOOK.md.\n")
sys.exit(0)
