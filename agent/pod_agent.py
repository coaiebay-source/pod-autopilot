"""
AG-UI agent endpoint for OpenBot.

OpenBot is not itself an agent -- it is the host. It gives each Bot a computer
(container + Chromium profile + workspace), a policy gateway, an audit trail,
channels, routines, and human takeover. You bring the agent: any endpoint
speaking the AG-UI protocol registers as a Bot.

So there are two deployment shapes, and you should understand which one you are
building:

  SHAPE A -- "managed coworker" (start here).
    You do NOT write an agent. You define Bots in the tenant YAML with a `role`
    (standing instructions) and grant them tools from pod.mcp_server. OpenBot's
    built-in managed agent (MANAGED_AGENT_AG_UI_URL) drives them with your model
    key. All the real logic lives in the MCP tools, which are deterministic
    Python. This is the right shape for this system, because the parts that must
    never be wrong -- gates, pricing, spend caps, IP clearance -- are already
    outside the model.

  SHAPE B -- "bring your own agent" (only if you need control flow the managed
    agent cannot express).
    You run a LangGraph / Mastra / CrewAI / Pydantic AI / Google ADK app that
    exposes an AG-UI endpoint, and register it under /agents in OpenBot. Tool
    calls it makes still route back through OpenBot's gateway, get policy-
    evaluated, audited, and only then executed. You keep your graph; OpenBot
    keeps the governance.

This file is a Shape B skeleton: a LangGraph agent with the POD tools bound,
served over AG-UI. Pin the versions you actually test against -- ag-ui and
langgraph are both moving fast and the integration surface has changed between
releases.

    pip install langgraph langchain-anthropic ag-ui-protocol ag-ui-langgraph
    python agent/pod_agent.py            # serves on :9000
    # then in OpenBot: /agents -> New -> AG-UI endpoint http://agent-harness:9000
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("pod.agent")

# ---------------------------------------------------------------------------
# Tool wrappers. Every one delegates to the deterministic pipeline. The agent
# decides WHICH to call and WITH WHAT; it never decides whether a gate passes.
# ---------------------------------------------------------------------------

def _pipeline():
    from pod.db import DB
    from pod.pipeline import Pipeline
    from pod.sentinel import Sentinel
    db = DB()
    return db, Pipeline(db), Sentinel(db)


def tool_sweep_demand(niche: str, concept: str, keywords: list[str], audience: str = "") -> dict:
    """Validate a concept against live demand data. Returns score, breakdown,
    corroborating sources, evidence URLs. A concept with <2 sources or score
    <0.55 is rejected -- your opinion is not evidence."""
    from pod import demand
    return demand.sweep(niche=niche, concept=concept, keywords=keywords, audience=audience).__dict__


def tool_screen_ip(concept: str, title: str, description: str, design_text: list[str],
                   product_type: str = "tshirt") -> dict:
    """Trademark/copyright clearance. FAILS CLOSED on error. Screen before
    building. A LOW verdict means no live Class 25 conflict was found in the
    registries checked -- it is not a legal opinion and does not cover artwork
    copyright or common-law marks."""
    from pod import trademark
    r = trademark.screen(concept=concept, title=title, description=description,
                         design_text=design_text, product_type=product_type)
    return {"verdict": "PASS" if r.risk == "LOW" else "REJECT", **r.to_dict()}


def tool_build_design(experiment_id: str, phrase: str, art_subject: str,
                      style: str = "retro_sunset", layout: str = "arch_top",
                      subphrase: str = "") -> dict:
    """Generate art (no text -- the model cannot render legible lettering) and
    composite a 4500x5400 300DPI transparent PNG with code-rendered typography."""
    from pod import design
    db, pipe, _ = _pipeline()
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": "unknown experiment"}
    brief = design.DesignBrief(concept=exp.concept, phrase=phrase, subphrase=subphrase,
                               niche=exp.niche, style=style, art_subject=art_subject,
                               layout=layout, product_type="tshirt",
                               audience_language=exp.keywords)
    ok = pipe.build(exp, brief)
    return {"ok": ok, "design_url": exp.design_file_url, "stage": exp.stage.value,
            "kill_reason": exp.kill_reason}


def tool_publish(experiment_id: str, phrase: str) -> dict:
    """Publish to Printful -> syncs to Square Online. Prices from live base cost,
    enforces the profit floor and listing caps, maps variations for attribution.
    You MUST then verify storefront visibility: Square's ecom_visibility is
    read-only via API, so a successfully created item can still be invisible."""
    db, pipe, sentinel = _pipeline()
    ok, why = sentinel.may_proceed()
    if not ok:
        return {"ok": False, "blocked_by_sentinel": why}
    exp = db.get(experiment_id)
    if not exp:
        return {"ok": False, "error": "unknown experiment"}
    published = pipe.publish(exp, phrase)
    return {"ok": published, "stage": exp.stage.value, "retail": exp.retail_price,
            "profit": exp.projected_profit, "square_item_id": exp.square_item_id,
            "NEXT_STEP": "verify storefront visibility in the browser"}


def tool_check_guardrails() -> dict:
    """Evaluate all hard thresholds. If hard_halt is true, call emergency_halt
    immediately -- do not investigate or attempt a fix first."""
    _, _, sentinel = _pipeline()
    v = sentinel.evaluate()
    return {"allowed": v.allowed, "hard_halt": v.hard_halt, "reasons": v.reasons}


def tool_emergency_halt(reason: str, delist_all: bool = False) -> dict:
    """STOP THE MACHINE. Human-only restart."""
    _, _, sentinel = _pipeline()
    return sentinel.emergency_halt(reason, delist_all=delist_all)


def tool_evaluate_tests() -> dict:
    """Verdicts on mature tests only (>=5 days AND >=800 impressions). Immature
    tests are held. Killed experiments are delisted from both systems."""
    db, pipe, sentinel = _pipeline()
    ok, why = sentinel.may_proceed()
    if not ok:
        return {"ok": False, "blocked_by_sentinel": why}
    return pipe.evaluate_tests()


def tool_daily_report() -> dict:
    """P&L, funnel, top kill reasons, guardrail state."""
    db, _, sentinel = _pipeline()
    with db.conn.cursor() as cur:
        cur.execute("SELECT stage, count(*) AS n FROM experiments GROUP BY stage")
        funnel = {r["stage"]: r["n"] for r in cur.fetchall()}
    return {"report": sentinel.daily_report(), "funnel": funnel, "pnl": db.pnl_snapshot()}


TOOLS = [
    tool_sweep_demand, tool_screen_ip, tool_build_design, tool_publish,
    tool_check_guardrails, tool_emergency_halt, tool_evaluate_tests,
    tool_daily_report,
]


# ---------------------------------------------------------------------------
# The agent. System prompt is deliberately short: the standing role comes from
# the tenant YAML, and the hard constraints live in the gates, not in prose.
# Prompts are suggestions. Code is a policy.
# ---------------------------------------------------------------------------

SYSTEM = """You are a print-on-demand operator running inside OpenBot.

Non-negotiable operating rules:

1. NEVER assert that something is trending. Call sweep_demand and report the
   numbers it returns. If it fails or returns no data, say UNVALIDATED. A
   confident claim with no evidence URL is worse than no claim, because a human
   downstream will believe it.

2. Screen IP BEFORE building. Never after. If screen_ip returns anything other
   than PASS, the concept is dead -- generate a different concept, do not
   misspell, stylize, or translate around the flagged term.

3. When you generate artwork, describe art only. Never request text, letters,
   or words from the image model. It cannot render legible lettering and will
   produce plausible garbage. The phrase goes in its own field and is typeset
   by code.

4. If check_guardrails returns hard_halt=true, call emergency_halt immediately.
   Do not investigate, do not attempt a fix, do not wait. Halting is the job.

5. After publishing, verify the listing is visible on the public storefront.
   The catalog API succeeding does not mean a customer can buy it.

6. Report failures as failures. A blocked action, a 429, a captcha, and a
   missing font are all different problems with different fixes. Never collapse
   them into "no results", because "no results" gets read as "no demand" and
   silently corrupts every downstream decision.
"""


def build_graph():
    """LangGraph react agent bound to the POD tools. Swap the model by changing
    BOT_PROVIDER / the init_chat_model string."""
    from langchain.agents import create_agent
    from langchain.chat_models import init_chat_model

    provider = os.environ.get("BOT_PROVIDER", "anthropic")
    model_name = os.environ.get("BOT_MODEL") or {
        "anthropic": "claude-sonnet-4-5",
        "openai": "gpt-5.5",
        "google": "gemini-2.5-flash",
    }[provider]
    model = init_chat_model(f"{provider}:{model_name}")
    return create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM)


def serve(host: str = "0.0.0.0", port: int = 9000) -> None:
    """Serve the graph over AG-UI so OpenBot can register it as a Bot.

    Bind 0.0.0.0 -- the container needs to be reachable from the OpenBot
    server. OpenBot validates agent endpoints with the same target checks it
    uses for browser navigation, so a private address needs to be listed in
    AGENT_ENDPOINT_ALLOWED_HOSTS.
    """
    try:
        from ag_ui_langgraph import LangGraphAgent as _LGAgent  # noqa: F401
        from ag_ui_langgraph.server import serve as ag_serve      # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "AG-UI LangGraph integration not installed.\n"
            "  pip install ag-ui-protocol ag-ui-langgraph\n"
            "Version pinning matters here: both packages move fast and the\n"
            "integration surface has changed between releases. Test against the\n"
            "OpenBot commit you deployed."
        ) from exc

    import uvicorn
    from ag_ui.core import Agent as AgentSpec  # type: ignore

    from fastapi import FastAPI
    app = FastAPI(title="pod-agent")
    graph = build_graph()

    # Minimal AG-UI shape: expose the graph as a runnable agent endpoint.
    # Consult ag-ui-langgraph's current README for the canonical wiring; this
    # is the structure, not a frozen API.
    from ag_ui_langgraph import LangGraphAgent

    agent = LangGraphAgent(
        name="pod-operator",
        description="Autonomous print-on-demand operator",
        graph=graph,
    )

    @app.post("/agent")
    async def run(input: dict[str, Any]):  # noqa: ANN401
        return await agent.run(input)

    @app.get("/health")
    async def health():
        return {"ok": True, "agent": "pod-operator"}

    log.info("Serving pod-agent on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve()
