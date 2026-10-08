"""
Optional raster art lane: Cloudflare Workers AI, FLUX.1 [schnell].

Free tier: 10,000 "neurons"/day on every Cloudflare account, no card. A
1024px FLUX schnell image costs on the order of $0.0006 of neurons, so the
daily free pool is roughly a hundred-plus images -- far more than the 6
designs/day guardrail. Use this when a concept really wants a painterly /
textured look that SVG can't give. It still produces 1024px, so the
Real-ESRGAN upscale step applies (free, CPU, slow-ish). Default lane is
SVG; this is opt-in via ART_PROVIDER=cloudflare.

Setup (one time, free):
  dash.cloudflare.com -> Workers & Pages -> get Account ID
  My Profile -> API Tokens -> Create -> "Workers AI" template -> token
  CF_ACCOUNT_ID=...  CF_API_TOKEN=...
"""

from __future__ import annotations

import base64
import logging
import os

import requests

log = logging.getLogger("pod.imagegen_cf")

MODEL = os.environ.get("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")


class CFImageError(Exception):
    pass


def generate(prompt: str, steps: int = 4) -> bytes:
    acct = os.environ.get("CF_ACCOUNT_ID")
    tok = os.environ.get("CF_API_TOKEN")
    if not (acct and tok):
        raise CFImageError("CF_ACCOUNT_ID / CF_API_TOKEN not set")
    r = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{MODEL}",
        headers={"Authorization": f"Bearer {tok}"},
        json={"prompt": prompt, "steps": steps},
        timeout=180,
    )
    if r.status_code == 429:
        raise CFImageError("Workers AI: daily free neurons exhausted or rate limited")
    if r.status_code >= 400:
        raise CFImageError(f"Workers AI {r.status_code}: {r.text[:300]}")
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("image/"):
        return r.content
    data = r.json()
    img = (data.get("result") or {}).get("image")
    if not img:
        raise CFImageError(f"unexpected response: {str(data)[:200]}")
    return base64.b64decode(img)
