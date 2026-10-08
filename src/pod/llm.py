"""
LLM access for the ZERO-CASH deployment.

The pipeline needs an LLM for exactly three judgment calls:
  1. turning raw trend data into concept candidates  (discover)
  2. writing a design brief + vector art (SVG)        (design)
  3. the weekly plain-English review                   (review)

Everything else is deterministic Python. So the LLM bill is small, and with
no cash we route it to things that are already paid for or free:

  provider="claude_code"   Your Claude Pro subscription, via the Claude Code
                           CLI in headless mode (`claude -p`). Locally it uses
                           your login; in GitHub Actions it uses the long-lived
                           token from `claude setup-token` (CLAUDE_CODE_OAUTH_TOKEN),
                           which Anthropic documents as supported for Pro/Max.
                           Usage counts against your Pro 5-hour window, not an
                           API bill. Best quality; budget ~2 runs/day.

  provider="gemini"        Google AI Studio free tier (no card). Hundreds of
                           requests/day on gemini-2.5-flash. Good enough for
                           scoring, titles, SVG. GEMINI_API_KEY.

  provider="github_models" GitHub Models, free with your GitHub account
                           (higher limits with Copilot Pro from the Student
                           Pack). OpenAI-compatible endpoint. ~50 req/day on
                           the big models, so use it as the fallback, not the
                           workhorse. Auth = a GitHub PAT with `models:read`.

  provider="codex"         ChatGPT Pro via the Codex CLI (`codex exec`).
                           Only usable where you have signed in interactively
                           once (your laptop / a VM you own) -- not GitHub
                           Actions. Very generous limits on Pro.

Chain: LLM_PROVIDER="claude_code,gemini,github_models" tries each in order
and falls through on rate-limit/auth failure. Nothing here can spend money:
every provider is subscription or free-tier, and the OpenAI/Anthropic *API*
keys are deliberately not consulted.

The LLM proposes; code disposes. Whatever comes back is parsed as JSON and
validated by the caller. Never let free text from here reach Square/Printful
without passing the trademark gate.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("pod.llm")


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str

    def json(self) -> Any:
        """Extract the first JSON object/array from the text (models love to
        wrap JSON in prose or fences)."""
        t = self.text.strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M)
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
        m = re.search(r"(\[.*\]|\{.*\})", t, flags=re.S)
        if not m:
            raise LLMError(f"{self.provider}: no JSON in response: {t[:200]!r}")
        return json.loads(m.group(1))


def providers() -> list[str]:
    raw = os.environ.get("LLM_PROVIDER", "claude_code,gemini,github_models")
    return [p.strip() for p in raw.split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Claude Code (Claude Pro subscription) -- headless
# ---------------------------------------------------------------------------

def _claude_code(prompt: str, system: str | None, max_turns: int) -> LLMResult:
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("claude CLI not installed (npm i -g @anthropic-ai/claude-code)")
    env = dict(os.environ)
    # Belt and braces: make sure a stray API key can't turn this into a bill.
    env.pop("ANTHROPIC_API_KEY", None)
    cmd = [
        exe, "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(max_turns),
        # No tools: this is a pure completion call. Tool use (browsing, shell)
        # happens in the separate agent step with its own allowlist.
        "--allowedTools", "",
        "--permission-mode", "dontAsk",
    ]
    if system:
        cmd += ["--append-system-prompt", system]
    model = os.environ.get("CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired as exc:
        raise LLMError("claude -p timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout)[:400]
        # Rate-limit / auth messages surface here; the chain falls through.
        raise LLMError(f"claude -p exit {proc.returncode}: {err}")
    try:
        data = json.loads(proc.stdout)
        text = data.get("result") if isinstance(data, dict) else proc.stdout
    except json.JSONDecodeError:
        text = proc.stdout
    return LLMResult(text=text or "", provider="claude_code", model=model or "default")


# ---------------------------------------------------------------------------
# Codex CLI (ChatGPT Pro subscription) -- headless, laptop/VM only
# ---------------------------------------------------------------------------

def _codex(prompt: str, system: str | None, max_turns: int) -> LLMResult:
    exe = shutil.which("codex")
    if not exe:
        raise LLMError("codex CLI not installed (npm i -g @openai/codex)")
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)  # force subscription auth, never API billing
    full = f"{system}\n\n{prompt}" if system else prompt
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as fh:
        out_path = fh.name
    cmd = [exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
           "-o", out_path, full]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired as exc:
        raise LLMError("codex exec timed out") from exc
    if proc.returncode != 0:
        raise LLMError(f"codex exec exit {proc.returncode}: {(proc.stderr or '')[:400]}")
    try:
        with open(out_path) as fh:
            text = fh.read()
    except OSError:
        text = proc.stdout
    return LLMResult(text=text, provider="codex", model="codex-default")


# ---------------------------------------------------------------------------
# Gemini (Google AI Studio free tier)
# ---------------------------------------------------------------------------

def _gemini(prompt: str, system: str | None, _: int) -> LLMResult:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY not set")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key}, json=body, timeout=180,
    )
    if r.status_code == 429:
        raise LLMError("gemini: rate limited (free tier RPD/RPM)")
    if r.status_code >= 400:
        raise LLMError(f"gemini {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError) as exc:
        raise LLMError(f"gemini: unexpected response {str(data)[:300]}") from exc
    return LLMResult(text=text, provider="gemini", model=model)


# ---------------------------------------------------------------------------
# GitHub Models (free with GitHub account; OpenAI-compatible)
# ---------------------------------------------------------------------------

def _github_models(prompt: str, system: str | None, _: int) -> LLMResult:
    token = os.environ.get("GH_MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise LLMError("GH_MODELS_TOKEN not set (PAT with models:read)")
    model = os.environ.get("GH_MODEL", "openai/gpt-4.1-mini")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    r = requests.post(
        "https://models.github.ai/inference/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/vnd.github+json"},
        json={"model": model, "messages": msgs, "temperature": 0.7, "max_tokens": 4000},
        timeout=180,
    )
    if r.status_code == 429:
        raise LLMError("github_models: rate limited")
    if r.status_code >= 400:
        raise LLMError(f"github_models {r.status_code}: {r.text[:300]}")
    text = r.json()["choices"][0]["message"]["content"]
    return LLMResult(text=text, provider="github_models", model=model)


_PROVIDERS = {
    "claude_code": _claude_code,
    "codex": _codex,
    "gemini": _gemini,
    "github_models": _github_models,
}


def complete(prompt: str, system: str | None = None, max_turns: int = 1) -> LLMResult:
    """Try providers in LLM_PROVIDER order; first success wins."""
    errors: list[str] = []
    for name in providers():
        fn = _PROVIDERS.get(name)
        if not fn:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            res = fn(prompt, system, max_turns)
            if not res.text.strip():
                raise LLMError(f"{name}: empty response")
            log.info("llm: %s/%s ok (%d chars)", res.provider, res.model, len(res.text))
            return res
        except LLMError as exc:
            log.warning("llm: %s failed -> %s", name, exc)
            errors.append(str(exc))
    raise LLMError("all providers failed: " + " | ".join(errors))


def complete_json(prompt: str, system: str | None = None) -> Any:
    sys_ = (system or "") + "\nRespond with JSON only. No prose, no markdown fences."
    return complete(prompt, sys_).json()
