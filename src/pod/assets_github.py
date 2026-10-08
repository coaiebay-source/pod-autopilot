"""
Free public hosting for print files: GitHub Release assets.

Printful fetches print files BY URL. The paid plan used Cloudflare R2. With
no card, the simplest always-free public URL you already own is a GitHub
release asset in your (private is fine) repo:

    https://github.com/<owner>/<repo>/releases/download/<tag>/<file>.png

Release assets on a private repo are NOT publicly downloadable, so this
module targets a dedicated PUBLIC "assets" repo (e.g. <you>/pod-assets).
That is fine: a print file is public the moment it is on a product page.

Housekeeping: Printful copies the file into its own File Library when the
sync product is created, so the GitHub copy is only needed for a few
minutes. `cleanup()` deletes assets older than ASSET_TTL_HOURS so the repo
never grows. Keep the SVG master in the private pipeline repo instead.

Auth: a fine-grained PAT scoped to the assets repo with Contents: read/write
(ASSETS_GH_TOKEN). In Actions you can also use a repo-scoped GITHUB_TOKEN if
the assets repo is the same repo -- but then it has to be public, so prefer
the two-repo layout.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("pod.assets_github")
API = "https://api.github.com"


class AssetError(Exception):
    pass


def _cfg() -> tuple[str, str, str]:
    repo = os.environ.get("ASSETS_GH_REPO", "")        # "owner/pod-assets"
    tok = os.environ.get("ASSETS_GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    tag = os.environ.get("ASSETS_GH_TAG", "printfiles")
    if not repo or not tok:
        raise AssetError("ASSETS_GH_REPO / ASSETS_GH_TOKEN not set")
    return repo, tok, tag


def _hdr(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _release(repo: str, tok: str, tag: str) -> dict:
    r = requests.get(f"{API}/repos/{repo}/releases/tags/{tag}", headers=_hdr(tok), timeout=30)
    if r.status_code == 200:
        return r.json()
    if r.status_code != 404:
        raise AssetError(f"get release {r.status_code}: {r.text[:200]}")
    r = requests.post(f"{API}/repos/{repo}/releases", headers=_hdr(tok), timeout=30,
                      json={"tag_name": tag, "name": "print files (transient)",
                            "body": "Auto-managed by pod-autopilot. Files expire.",
                            "draft": False, "prerelease": True})
    if r.status_code >= 300:
        raise AssetError(f"create release {r.status_code}: {r.text[:200]}")
    return r.json()


def upload(path: Path) -> str:
    """Upload a file and return its public download URL."""
    repo, tok, tag = _cfg()
    rel = _release(repo, tok, tag)
    name = path.name
    # Delete a same-named asset first (re-runs).
    for a in rel.get("assets", []):
        if a["name"] == name:
            requests.delete(a["url"], headers=_hdr(tok), timeout=30)
    upload_url = rel["upload_url"].split("{", 1)[0]
    with open(path, "rb") as fh:
        r = requests.post(upload_url, params={"name": name},
                          headers={**_hdr(tok), "Content-Type": "image/png"},
                          data=fh, timeout=300)
    if r.status_code >= 300:
        raise AssetError(f"upload {r.status_code}: {r.text[:200]}")
    url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    # Verify it is actually fetchable anonymously -- Printful will be anonymous.
    chk = requests.head(url, allow_redirects=True, timeout=30)
    if chk.status_code != 200:
        raise AssetError(f"asset not publicly reachable ({chk.status_code}): is {repo} public?")
    log.info("asset published %s", url)
    return url


def cleanup(ttl_hours: int | None = None) -> int:
    """Delete release assets older than TTL. Returns number removed."""
    repo, tok, tag = _cfg()
    ttl = ttl_hours or int(os.environ.get("ASSET_TTL_HOURS", "48"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl)
    rel = _release(repo, tok, tag)
    n = 0
    for a in rel.get("assets", []):
        created = datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
        if created < cutoff:
            r = requests.delete(a["url"], headers=_hdr(tok), timeout=30)
            if r.status_code in (204, 404):
                n += 1
    if n:
        log.info("asset cleanup: removed %d file(s) older than %dh", n, ttl)
    return n
