# POD Autopilot

> **No cash? Start with [`docs/ZERO_CASH_PLAN.md`](docs/ZERO_CASH_PLAN.md).**
> Same pipeline on free plumbing: GitHub Actions as the scheduler, Claude Pro /
> Gemini free tier as the brain, LLM-written vector art instead of a paid image
> model, Neon for Postgres, polling instead of webhooks, Pinterest + a free
> click counter instead of ads. `make zero-demo` proves it offline.



A fully autonomous print-on-demand operation on **Square Online + Printful**, run by
**OpenBot** (CopilotKit's self-hosted governed agent platform).

It discovers demand, generates designs, screens them for IP, publishes, tests,
scales winners and kills losers — with hard numeric guardrails and a kill switch
that overrides everything.

**Start here, in this order:**
1. [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) — what the system is and why it's shaped this way
2. [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — how to actually build and run it, day by day, plus the operating rhythm
3. `make validate` — then nothing else until it says 88/88

---

## The one-paragraph architecture

```
OpenBot (governance: policy gateway, audit, per-bot computers, routines, takeover)
  ├─ TrendScout      browser + demand tools      finds and VALIDATES demand
  ├─ DesignDirector  design tools                phrase + art direction
  ├─ IPGuard         browser + clearance tools   FAIL-CLOSED trademark screen
  ├─ CatalogBot      shell + browser             publishes; flips Square visibility
  └─ OpsSentinel     shell + browser + halt      guardrails, test verdicts, kill switch
        │  every action via policy, audited
        ▼
pod-pipeline (deterministic Python, MCP server)   ← gates, pricing, state machine
        │
   ┌────┴─────────────┐
   ▼                  ▼
Printful API       Square Catalog API + Webhooks
(print files,      (listing, sale, order events)
 mockups, sync      ecom_visibility: READ-ONLY ← the one browser-only action
 product = push
 to Square)
```

The rule that makes "autonomous" survivable: **the LLM proposes, code disposes.**
Every gate — demand corroboration, IP clearance, print-file dimensions, mockup
count, profit floor, storefront visibility, test maturity, spend caps — is
evaluated in Python, not in a prompt. A prompt can be talked past. A gate cannot.

## Layout

```
docs/BLUEPRINT.md            the full breakdown + play-by-play  ← READ THIS
src/pod/
  state.py                   experiment state machine + gates
  sentinel.py                guardrails + kill switch
  pipeline.py                stage orchestration (score → ip → design → publish → test)
  demand.py                  demand discovery + scoring (multi-source, corroborated)
  trademark.py               IP clearance, fail-closed
  design.py                  art-gen + code-rendered typography → 4500x5400 @300DPI PNG
  printful.py                Printful client with real rate-limit handling
  square.py                  Square client + webhook signature verification
  webhooks.py                Square/Printful event receiver (sales attribution)
  db.py / memory_db.py       Postgres store / offline test store
  mcp_server.py              the tools OpenBot Bots are granted
  cli.py                     validate / run-one / report / halt / resume
agent/pod_agent.py           AG-UI agent endpoint (Shape B deployment)
openbot/
  policy.json                deny-before-allow action policy
  tenant/pod-autopilot.yaml  bots, roles, grants, routines
sql/schema.sql               experiments, sales, spend, events, ip_strikes + views
scripts/validate.py          88 offline checks. All must pass before deploying.
fonts/Anton-Regular.ttf      OFL-licensed, commercial embedding OK
```

## Quick start (safe order)

```bash
pip install -e ".[dev]"
python -m pod.cli validate          # 88 checks, no keys, no network
psql pod -f sql/schema.sql          # after creating the db

# Then, still fully offline:
DRY_RUN=1 ASSET_BASE_URL=https://assets.example.test \
  python -m pod.cli run-one \
    --niche nursing \
    --concept "Night Shift Nurse Coffee" \
    --phrase "STILL RUNNING ON CAFFEINE" \
    --keywords "night shift nurse,nurse coffee,nurse life" \
    --style retro_sunset \
    --art-subject "a steaming coffee cup silhouette under a starry night sky"
```

Flip `DRY_RUN=0` and `SQUARE_SANDBOX=1` for a sandboxed live run. Only after that
behaves correctly, go production — with the guardrail caps in `.env.example`
left TIGHT.

## The four things that will actually break you

1. **Square `ecom_visibility` is read-only.** No API can make an item visible on
   Square Online. Set the dashboard default once
   (*Dashboard → Online → Items → Item Sync → Item visibility → Visible*), and
   let CatalogBot verify against the live storefront after each publish.
2. **Image models cannot render text.** Art and typography are generated
   separately and composited. See `src/pod/design.py`.
3. **Printful NEEDS_APPROVAL orders.** Customer paid, nothing printing. The
   Sentinel treats ≥1 as a hard halt.
4. **Unverifiable "trends".** An LLM asserting something is popular is
   confabulating. Every concept needs ≥2 machine-captured signals with evidence
   URLs or it dies at the gate.
