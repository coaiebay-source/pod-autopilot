"""
Central configuration for the POD Autopilot system.

Every secret comes from the environment. Nothing is hardcoded. In an OpenBot
deployment these live in /admin/credentials (write-only, encrypted at rest) and
are injected into the Bot's computer container -- they never enter a transcript.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in OpenBot admin credentials or your .env file."
        )
    return val or ""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

PRINTFUL_API = "https://api.printful.com"
PRINTFUL_API_V2 = "https://api.printful.com/v2"
SQUARE_API = os.environ.get("SQUARE_ENV_URL", "https://connect.squareup.com")
SQUARE_SANDBOX_API = "https://connect.squareupsandbox.com"

# Square API version pin. Square requires this header on every call.
# Bump deliberately, never float it -- a silent version change can break
# an unattended pipeline.
SQUARE_VERSION = os.environ.get("SQUARE_API_VERSION", "2026-09-16")


# ---------------------------------------------------------------------------
# Design / print specs
# ---------------------------------------------------------------------------
# Printful DTG: 4500x5400 px at 300 DPI, transparent PNG, sRGB, max 200 MB.
# Always author at the print-file size. Never upscale a small render to fake
# resolution -- Printful accepts it and the customer gets a blurry shirt.
@dataclass(frozen=True)
class PrintSpec:
    product_slug: str
    width: int
    height: int
    dpi: int = 300
    fmt: str = "png"

    @property
    def inches(self) -> tuple[float, float]:
        return self.width / self.dpi, self.height / self.dpi


PRINT_SPECS: dict[str, PrintSpec] = {
    # Standard DTG tee front. The Bella+Canvas 3001 (Printful product id 71)
    # uses exactly this.
    "tshirt_front": PrintSpec("tshirt_front", 4500, 5400, 300),
    "hoodie_front": PrintSpec("hoodie_front", 4800, 6000, 300),
    "mug_11oz": PrintSpec("mug_11oz", 2475, 1155, 300),
    "mug_15oz": PrintSpec("mug_15oz", 2790, 1365, 300),
    "poster_18x24": PrintSpec("poster_18x24", 5400, 7200, 300),
    "tote": PrintSpec("tote", 4200, 4200, 300),
}

MAX_PRINT_FILE_MB = 200


# ---------------------------------------------------------------------------
# Rate limits (Printful)
# ---------------------------------------------------------------------------
# General API: 120 req/min (leaky bucket). Mockup task creation is the tight
# one: 2 req/min for a brand-new store, 10 req/min once the store has at least
# $10 of fulfilled orders. Daily cap: 20,000 generated files per account.
PRINTFUL_RPM_GENERAL = _env_int("PRINTFUL_RPM_GENERAL", 120)
PRINTFUL_RPM_MOCKUP = _env_int("PRINTFUL_RPM_MOCKUP", 2)
PRINTFUL_DAILY_MOCKUP_FILES = _env_int("PRINTFUL_DAILY_MOCKUP_FILES", 20000)
# Stay under the ceiling, not on it. A 429 costs a 60s lockout.
RATE_LIMIT_SAFETY = _env_float("RATE_LIMIT_SAFETY", 0.8)


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------
@dataclass
class Economics:
    """Unit economics for a single listing. All money in USD."""

    # Square Online free plan: 3.3% + $0.30 per online transaction.
    # Plus plan ($49/mo): 2.9% + $0.30. Premium ($149/mo): 2.6% + $0.30.
    square_fee_pct: float = _env_float("SQUARE_FEE_PCT", 0.033)
    square_fee_fixed: float = _env_float("SQUARE_FEE_FIXED", 0.30)

    # Blended shipping you eat or pass through. Printful charges it to you.
    shipping_avg: float = _env_float("SHIPPING_AVG", 4.50)

    # Target contribution margin AFTER all fees. Below this, don't list.
    target_margin: float = _env_float("TARGET_MARGIN", 0.30)

    # Floor. Never list below this profit per unit, no matter the trend score.
    min_profit: float = _env_float("MIN_PROFIT", 8.00)

    def retail_for(self, base_cost: float) -> float:
        """
        Solve for the retail price that yields `target_margin` contribution.

        profit = retail - base - shipping - (retail * pct) - fixed
        margin = profit / retail
        => retail = (base + shipping + fixed) / (1 - pct - margin)
        """
        denom = 1.0 - self.square_fee_pct - self.target_margin
        if denom <= 0:
            raise ValueError("Fees plus target margin exceed 100% -- impossible price")
        price = (base_cost + self.shipping_avg + self.square_fee_fixed) / denom
        return round_to_99(price)

    def profit_at(self, retail: float, base_cost: float) -> float:
        fees = retail * self.square_fee_pct + self.square_fee_fixed
        return retail - base_cost - self.shipping_avg - fees

    def viable(self, retail: float, base_cost: float) -> bool:
        return self.profit_at(retail, base_cost) >= self.min_profit


def round_to_99(price: float) -> float:
    """Charm pricing: $21.37 -> $21.99. Cheap conversion lift, and it keeps
    the catalog looking intentional instead of machine-generated."""
    base = int(price)
    candidate = base + 0.99
    if candidate < price:
        candidate = base + 1 + 0.99
    return round(candidate, 2)


# ---------------------------------------------------------------------------
# Kill-switch thresholds. These are HARD limits, not suggestions.
# Checked by the Sentinel bot before every spend-side action and on a timer.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Guardrails:
    weekly_fulfillment_cap: float = _env_float("CAP_WEEKLY_FULFILLMENT", 250.00)
    weekly_ad_cap: float = _env_float("CAP_WEEKLY_ADS", 70.00)
    daily_ad_cap: float = _env_float("CAP_DAILY_ADS", 10.00)
    max_new_listings_per_day: int = _env_int("CAP_NEW_LISTINGS_DAY", 6)
    max_concurrent_tests: int = _env_int("CAP_CONCURRENT_TESTS", 10)
    # A single takedown notice is enough to stop the whole machine. An
    # automated design pipeline that ships an infringing design is the fastest
    # way to lose a Square account permanently.
    ip_strikes_halt: int = _env_int("CAP_IP_STRIKES", 1)
    refund_rate_halt: float = _env_float("CAP_REFUND_RATE", 0.08)
    min_test_days: int = _env_int("MIN_TEST_DAYS", 5)
    min_test_impressions: int = _env_int("MIN_TEST_IMPRESSIONS", 800)
    # ---- ZERO-CASH / ORGANIC TEST MODE ------------------------------------
    # TRAFFIC_MODE=organic: no ads, so there are no impression counts. Tests
    # are judged on tracked clicks (click-counter Worker) and orders over a
    # longer window. Slower, free, and honest about its own noise.
    traffic_mode: str = _env("TRAFFIC_MODE", "paid").lower()
    organic_min_days: int = _env_int("ORGANIC_MIN_DAYS", 14)
    organic_min_clicks: int = _env_int("ORGANIC_MIN_CLICKS", 40)
    organic_max_days: int = _env_int("ORGANIC_MAX_DAYS", 35)     # decide no matter what
    organic_min_cvr: float = _env_float("ORGANIC_MIN_CVR", 0.01)  # orders / clicks


GUARDRAILS = Guardrails()
ECON = Economics()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@dataclass
class Credentials:
    printful_token: str = field(default_factory=lambda: _env("PRINTFUL_API_TOKEN"))
    square_token: str = field(default_factory=lambda: _env("SQUARE_ACCESS_TOKEN"))
    square_location_id: str = field(default_factory=lambda: _env("SQUARE_LOCATION_ID"))
    openai_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    anthropic_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    apify_token: str = field(default_factory=lambda: _env("APIFY_TOKEN"))
    serpapi_key: str = field(default_factory=lambda: _env("SERPAPI_KEY"))
    # Public base URL where design PNGs are hosted. Printful fetches print
    # files and mockup source images BY URL -- it cannot take an upload stream
    # from a private host. Cloudflare R2/S3 with a public read bucket is the
    # usual answer; it costs pennies.
    asset_base_url: str = field(default_factory=lambda: _env(
        "ASSET_BASE_URL", required=not bool(os.environ.get("ASSETS_GH_REPO"))))
    asset_dir: Path = field(default_factory=lambda: Path(_env("ASSET_DIR", "./assets")))
    webhook_secret: str = field(default_factory=lambda: _env("SQUARE_WEBHOOK_SIGNATURE_KEY"))
    db_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", "postgresql://pod:pod@localhost:5433/pod"
        )
    )

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(f"Missing credentials: {', '.join(missing)}")


CREDS = Credentials()


# ---------------------------------------------------------------------------
# Sandbox / dry-run
# ---------------------------------------------------------------------------
# Build the ENTIRE pipeline in dry-run first. DRY_RUN=1 makes every write
# (Printful product creation, Square catalog upsert, ad spend) log the payload
# and return a fake id instead of calling out. You want to watch 20 fake
# designs flow end-to-end before a single real dollar moves.
DRY_RUN = os.environ.get("DRY_RUN", "0") not in ("0", "false", "False", "")
USE_SANDBOX = os.environ.get("SQUARE_SANDBOX", "0") not in ("0", "false", "False", "")

# Design generation model. Pick one; the engine adapts.
IMAGE_MODEL = _env("IMAGE_MODEL", "gpt-image-1")
TEXT_MODEL = _env("TEXT_MODEL", "gpt-5.5")

# Fonts must be licensed for commercial use. Do NOT ship a system font into a
# product you sell -- font licensing is a real and frequently-ignored POD
# liability. Buy a commercial license or use an OFL font.
FONT_DIR = Path(_env("FONT_DIR", "./fonts"))
