# THE BLUEPRINT
## Fully autonomous print-on-demand: Square Online + Printful, run by OpenBot

> Demand → design → clearance → publish → test → scale or kill, on a loop,
> with hard guardrails and a kill switch that overrides everything.

Everything in this document is wired to runnable code in this repo. Where the
code exists, the doc says so and points at the file. Where something is a
dashboard click, the doc gives the exact click path. Where something is a
judgment call with no right answer, the doc says that too and gives you the
starting prior I used and why.

---

## Part 0 — What you're actually building, and the six constraints that shape it

Before any play-by-play, you need the constraints, because every unusual
decision in this design exists to satisfy one of them.

### Constraint 1: Square cannot set online-store visibility via API

Square's `item_data.ecom_visibility` and `ecom_available` fields are *returned*
by the Catalog API but are **read-only**. Square staff confirmed this on their
developer forum; the legacy `available_online` field predates modern Square
Online and is not reliable.

Consequence: you can create a perfect catalog item through the API and it will
sit in the Item Library, invisible to every buyer. This is the #1 silent killer
of Square POD automation, and it is also **the real reason OpenBot is in this
architecture** — the browser is not a gimmick, it is the only programmatic way
to flip that bit.

Mitigations, use both:
1. **Set the default once by hand:** *Square Dashboard → Online → Items → Item
   Sync → Item visibility settings → Visible.* After that, API-created and
   Printful-pushed items go live by default. Handles ~95% of cases.
2. **Verify against the live storefront after every publish.** Ground truth is
   the public site, not the catalog API response. If missing:
   *Dashboard → Online → Items → Site Items → item → Site visibility → Visible →
   Save.* That flip is the CatalogBot's browser job.

### Constraint 2: Image models cannot render text

Ask any diffusion model for "a tee design that says STILL RUNNING ON CAFFEINE"
and 19 times in 20 you get *STILL RUNNIG ON CAFFElNE*. It renders glyph shapes,
not characters. At 40 designs a day that is a return-rate catastrophe.

Fix — **separate art from typography:**
1. Image model generates **art only**; the prompt explicitly forbids text and
   the code verifies that clause is present before sending.
2. The phrase is typeset by Pillow with a licensed font at full print
   resolution. Every character exact by construction.
3. Composite onto the 4500×5400 @ 300 DPI transparent canvas.

You can see a working example of exactly this in `artifacts/` — the phrase on
that preview shirt was rendered by code, which is why it is spelled correctly.

### Constraint 3: Printful's mockup generator is your throughput bottleneck

- General API: 120 req/min (leaky bucket).
- **Mockup task creation: 2 req/min on a new store**, 10 req/min once the store
  has ≥$10 of fulfilled orders. Plus 20,000 generated files/day.
- A 429 costs a **60-second lockout of the whole endpoint**.

Consequence: 5 variants ≈ 3 minutes minimum per design, and aggressive retrying
makes it worse, not better. The client implements a matching leaky bucket, reads
the live `X-Ratelimit-*` headers, and honors `Retry-After`. Budget your daily
cadence around this: **2 req/min ≈ 6 designs/hour of mockup time.**

### Constraint 4: Printful→Square sync is the listing mechanism

You do not push products to Square separately. Connect Printful to Square once;
then `POST /store/products` (a "sync product") **is** the publish action — it
creates the Printful product and pushes the listing, mockups, variants and
retail price to Square Online. Orders flow back automatically: customer pays on
Square → Printful imports the order → charges your billing method → prints →
ships → pushes tracking to Square. There is no manual step anywhere in that
loop, which is what makes POD automatable at all.

### Constraint 5: An LLM asserting "this is trending" is confabulating

It has no live data. It will produce a confident, plausible, fabricated trend
report. So the system is built so it *cannot* trust one: a concept may not
advance past SCORED without **≥2 machine-captured signals from different
sources, each with a saved evidence URL**. The LLM proposes concepts; a
deterministic sweep disposes of them.

### Constraint 6: At autonomous volume, IP failures are guaranteed without a gate

A human shop eyeballs 5 designs a week. An automated shop ships 40. Trademarked
phrases that *feel* generic ("Mama Bear", "Best Dad Ever", "Girl Boss") have
live registrations in Class 25. Sports leagues, franchises, and celebrities are
the fastest way to lose a merchant account. The IP gate **fails closed**: if the
screen errors, times out, or returns anything other than LOW risk, the design
dies. Threshold for a full halt on a real external strike: **one**.

### The resulting rule

> **The LLM proposes. Code disposes.**
> Every gate — corroboration, IP, print dimensions, mockup count, profit floor,
> storefront visibility, test maturity, spend caps — is evaluated in Python
> (`src/pod/state.py`, `sentinel.py`). A prompt can be talked past. A gate
> cannot. That single rule is the difference between "autonomous" and
> "unattended with a wallet attached."

---

## Part 1 — Architecture

```
┌────────────────────────── OpenBot (your infrastructure) ─────────────────────────┐
│  policy gateway (CEL, deny-before-allow, fail-closed) → audit row → execute      │
│                                                                                  │
│  TrendScout        browser + demand tools      validates demand, captures       │
│                                                  evidence URLs                   │
│  DesignDirector    design tools                phrase + art direction + style   │
│  IPGuard           browser + clearance         FAIL-CLOSED trademark screen     │
│  CatalogBot        shell + browser             publish; verify/flip visibility  │
│  OpsSentinel       shell + browser + halt      guardrails, verdicts, kill switch│
│                                                                                  │
│  routines (cron) · per-Bot containers · human takeover at 2FA · encrypted creds │
└───────────────┬──────────────────────────────────────────────────────────────────┘
                │ MCP tool calls, each policy-checked and audited
                ▼
┌────────────────────────── pod-pipeline (deterministic Python) ───────────────────┐
│  state machine · gates · pricing · Sentinel · Postgres                           │
│  src/pod/mcp_server.py exposes exactly the tools each Bot is granted             │
└───────┬──────────────────────────┬──────────────────────────────┬───────────────┘
        ▼                          ▼                              ▼
  Printful API               Square Catalog API            Square / Printful
  print files, mockups,      batch-upsert items,           webhooks → attribution
  sync products (the         search, images                + guardrail inputs
  publish action)
```

**Two deployment shapes.** OpenBot is a host, not an agent — it supplies the
computers, the governance, the channels and the scheduling; you supply the
agent.

- **Shape A (use this):** don't write an agent. Define Bots in the tenant YAML
  with standing roles, grant them tools from `pod.mcp_server`. OpenBot's managed
  agent drives them with your model key. All the logic that must never be wrong
  already lives outside any model.
- **Shape B (only if you need custom control flow):** run your own LangGraph /
  Mastra / CrewAI / ADK app exposing an AG-UI endpoint (`agent/pod_agent.py` is
  a skeleton) and register it under `/agents`. Its tool calls still route
  through OpenBot's gateway, get policy-evaluated and audited.

**Why per-Bot computers matter here specifically:** TrendScout browses hostile
third-party pages (that's where prompt injection arrives). CatalogBot holds a
live Square session. If they share one browser profile, an injected instruction
on a research page is one hop from your store dashboard. Isolate.

---

## Part 2 — Phase-by-phase play-by-play

### Phase 0 · Accounts and the one-time dashboard setup (≈45 min, human)

**Square**
1. Dashboard → *Developer Dashboard* → create an application. Note the
   Application ID/Secret.
2. Under your app → *Personal Access Token* → create for **Production**.
   Scopes: `ITEMS_WRITE ITEMS_READ ORDERS_READ PAYMENTS_READ
   MERCHANT_PROFILE_READ` (+ `CUSTOMERS_WRITE` optional).
   A PAT beats OAuth here: single seller, self-hosted, no consent flow to
   automate.
3. **The critical click:** *Dashboard → Online → Items → Item Sync → Item
   visibility settings → Visible.* (Constraint 1.)
4. *Dashboard → Online → Settings → Square Sync* → confirm **"Mark newly
   imported items as unavailable online" is OFF.** Printful's own docs call this
   out; leaving it on means every product you publish lands dead.
5. Confirm your Square Online site is published and you know its public URL —
   the CatalogBot verifies listings against it.
6. Plan check: Free plan = **3.3% + 30¢** per online transaction. Plus ($49/mo)
   = 2.9% + 30¢, Premium ($149/mo) = 2.6% + 30¢. Start Free; the economics
   function in `config.py` reads these from env so a plan change is a one-line
   edit, not a code change.

**Printful**
1. Dashboard → *Stores → Choose platform → Square → Connect to Square → Allow.*
2. *Billing → Billing methods* → add a card. **Without this, order import
   silently stops** — Printful can't charge you, so it won't print. The single
   most common "why did everything stop" in POD automation.
3. *Stores → <your store> → Edit → Orders* → confirm **"Manually confirm
   imported orders" is OFF.** If it's on, every order parks in NEEDS_APPROVAL:
   the customer paid and nothing is printing. The Sentinel treats ≥1 as a hard
   halt because that is exactly what it is.
4. *Settings → API* → generate a store token. Note your store's product ids:
   call `GET /products/71` and record the real variant ids for the colors/sizes
   you'll sell. `pipeline.py` ships with the common BC3001 ids; **verify them**,
   Printful changes catalog ids.
5. *Settings → Webhooks* → add
   `https://<your-host>/hooks/printful/<PRINTFUL_HOOK_SECRET>`, subscribe to
   order and shipment events.

**Asset hosting.** Printful fetches print files **by URL** — it cannot take an
upload stream from a private host. Create a Cloudflare R2 bucket with public
read + custom domain (pennies at this volume), or an S3 bucket + CloudFront.
Set `ASSET_BASE_URL`. `design.upload_asset()` assumes the dir is served; swap in
the R2/S3 SDK for a real deployment.

**USPTO clearance.** Create an Apify account and note the token
(`dev00/uspto-trademark-text-check-api`, ~$5 per 1,000 text checks ≈ $3/month at
6 designs/day). This is the cheapest insurance in the system.

### Phase 1 · Deploy OpenBot (≈30 min)

Requirements: Docker, Bun 1.3+, a CopilotKit Intelligence license token, and a
model key (nothing ships in the box — the model is yours).

```bash
git clone https://github.com/CopilotKit/OpenBot && cd OpenBot
cp .env.example .env                 # then edit
# paste openbot/policy.json content into AGENT_COMPUTER_POLICY
# point TENANT_PACKAGE_DIR at pod-autopilot/openbot/tenant
bash scripts/start.sh
# open http://localhost:3010
```

`.env` essentials (full annotated version at `pod-autopilot/.env.example`):

```bash
OPENBOT_SINGLE_USER=true            # required w/o an IdP ⇒ NEVER expose this port
KEY_ENCRYPTION_KEY=<openssl rand -hex 32>
COPILOTKIT_LICENSE_TOKEN=...
BOT_PROVIDER=anthropic
COMPUTER_SUPERVISOR_URL=http://supervisor:4300   # one computer per Bot
COMPUTER_RUNTIME=runsc              # gVisor, where the host supports it
COMPUTER_SANDBOX=on
AGENT_COMPUTER_POLICY_FILE=.../policy.json
```

**Security posture, non-negotiable:** OpenBot is alpha software with a
documented no-auth default and open authorization issues. Keep the UI on
localhost / Tailscale / a VPN. The shipped startup policy **allows all actions**
— replace it with `openbot/policy.json` before the first Bot runs. Malformed
policy JSON stops server startup, which is the behavior you want.

Then in the UI:
- `/settings/connected-accounts` and `/settings/passwords` — sign the Bots in.
  Saved credentials are encrypted at rest and never enter a transcript.
- `/admin/credentials` — add `PRINTFUL_API_TOKEN`, `SQUARE_ACCESS_TOKEN`,
  `OPENAI_API_KEY`, `APIFY_TOKEN`, `ASSET_R2_KEY`.
- `/agents` — create the five coworkers from `openbot/tenant/pod-autopilot.yaml`
  (or rely on the tenant package if your pinned version loads it; reconcile the
  YAML field names against `examples/fintech/` in your commit — the shape has
  moved between alpha releases, and the server refuses to boot on a mismatch,
  which is good).

### Phase 2 · The policy file (15 min, and the highest-leverage 15 minutes)

`openbot/policy.json` is deny-before-allow, default-deny. The load-bearing rules:

| Rule | Why |
|---|---|
| deny any `squareup.com` billing / bank / payment-method URL | a bot that can edit billing can drain the account |
| deny `printful.com/billing` | same |
| deny Printful store *settings* pages | flipping "manually confirm orders" silently stops all fulfillment |
| deny `developer.squareup.com` | tokens & webhooks are infrastructure |
| deny navigation outside the research/storefront allowlist | prompt injection arrives as a web page; confinement is the containment |
| deny shell except `python -m pod.*` | shell is the escape hatch from every other rule |
| deny reads of `.env` / credentials / `.ssh` | secrets never enter a transcript |
| allow CatalogBot on `/items` + `/online` pages | **the** visibility flip — the one action no API covers |
| allow OpsSentinel on Printful orders page | triage NEEDS_APPROVAL (read + the single benign unstick action) |
| allow TrendScout on trends/etsy-search/amazon-bsr/reddit/pinterest | demand research, nothing else |

Field names in CEL rules have moved between OpenBot alpha releases — verify
against `docs/policy.md` in the exact commit you deploy. The structure and the
intent are the durable part.

### Phase 3 · Database, fonts, offline validation (≈20 min)

```bash
createdb pod && psql pod -f sql/schema.sql
# schema gives you: experiments (state machine rows), exp_variations
# (sale→experiment attribution), sales (append-only, idempotent on order_id),
# spend (guardrail inputs), events, ip_strikes, plus v_pnl / v_funnel /
# v_kill_reasons views.

pip install -e ".[dev]"
python -m pod.cli validate        # 88 checks. no keys, no network, no Postgres
```

`scripts/validate.py` proves the things that would otherwise cost you money to
discover: illegal transitions refused (including DISCOVERED→LISTED), IP gate
fails closed on *every* uncertainty path, a mature test with 0 orders is killed
while an immature one is held, 30% refunds kills a *converting* design, each
guardrail halts, emergency halt stops everything, sales attribute and refunds
roll up, dedupe stops the loop rebuilding yesterday's shirt, and the design
engine emits a 4500×5400 @300 DPI transparent PNG with exact typography.

**All 88 must pass before anything touches a live account.** If a check fails
after you change something, that change is not safe to deploy.

Download an OFL font into `fonts/` (Anton and Bebas Neue are already good;
the repo ships Anton). **Font licensing is a real POD liability** — a font
licensed for desktop use is not licensed for embedding in merchandise you
sell. OFL faces are free for exactly this.

### Phase 4 · The five Bots (what each one does, and on what schedule)

| Bot | Grants | Standing job | Routine |
|---|---|---|---|
| **TrendScout** | browser, demand tools | Propose concepts from your niche list, then *validate*: sweep Google Trends (momentum + breakout related-queries), Etsy result counts (competition density), Amazon BSR (purchase evidence, browser-only), Reddit hot pages (audience depth + vocabulary). Record evidence URLs. Never assert. | daily 13:00 |
| **DesignDirector** | design tools only | Phrase + art direction + style from the fixed library + layout. Screen IP **before** building. Never put words in the art prompt. | daily 13:30 |
| **IPGuard** | browser, clearance tools | Independent second screen, bias toward rejection: Class 25 (+21/16/18 per product), live *and pending* marks, celebrity/team/franchise/lyric patterns. Error ⇒ REJECT. | daily 14:00 |
| **CatalogBot** | shell + browser | Publish cleared designs (mechanics via API), then verify storefront visibility; flip Site visibility only when verification fails. | daily 14:30 |
| **OpsSentinel** | shell + browser + halt | Guardrails every 15 min; test verdicts daily; delist the killed; daily P&L to you. | 15-min + daily |

OpenBot constraints worth knowing: routines have a **15-minute floor** and a
**20-enabled cap**, and ten consecutive failures auto-disable a routine. The
15-min guardrail check is the one routine that genuinely wants the floor — it's
the thing that catches a runaway spend loop within a quarter hour.

### Phase 5 · One design, end to end (the play-by-play)

**Stage 0 — DISCOVERED.** TrendScout proposes, e.g.
niche `nursing`, concept `night shift nurse coffee`, keywords
`night shift nurse, nurse coffee, 12 hour shift`.

**Stage 1 — SCORED.** `pod.sweep_demand` runs:
- Google Trends 3-month interest: momentum (Δ vs baseline) + magnitude +
  breakout flag.
- Rising related queries — this is where the *phrase* comes from; a "Breakout"
  related query is the highest-value signal in the whole system.
- Etsy listing count → competition gap on a log scale. **High interest + low
  competition is the gap you want.** High interest + 40,000 listings is the
  40,001st shirt.
- Reddit median hot-post engagement → audience depth.
- Amazon BSR (supplied by the browser step) → purchase evidence.

Score = .30·momentum + .20·magnitude + .20·gap + .15·purchase + .15·depth.
Gate: **≥2 corroborating sources AND score ≥0.55.** Fewer sources = refused,
because one source is indistinguishable from hallucination. (Weights are a
prior; refit from your own outcomes after ~50 experiments.)

**Stage 2 — IP_CLEARED.** Layers, cheapest first, short-circuit on first hit:
local blocklist (+ structural patterns like `est. YYYY`, `in my _ era`) → USPTO
TESS Class 25 via Apify → reverse-image (currently **not wired**, so the screen
reports it and the gate treats artwork originality as unverified — wire TinEye
or a Lens browser step before you trust this layer). Gate: LOW only. Anything
else, including "the checker was down," kills the design.

**Stage 3 — DESIGNED.** Art-only generation → white-backdrop knockout to real
alpha (DTG prints on colored garments; a white box around your art prints as a
white box) → typography composited by Pillow (arched for short phrases, straight
for long) → 4500×5400 @300 DPI transparent PNG, DPI embedded in metadata
(Printful warns on 72-DPI metadata even when pixels are right) → uploaded to
`ASSET_BASE_URL`.

**Stage 4 — MOCKED_UP.** `GET /mockup-generator/printfiles/71` for real print
areas → `POST /mockup-generator/create-task/71` with your variant ids → poll the
task → collect mockup URLs. Gate: **≥3 mockups** — one flat image converts like
a listing nobody trusts. This stage takes ~3 min per design at new-store rates.

**Stage 5 — LISTED.** Price from *live* Printful base cost:
`retail = (base + shipping + 0.30) / (1 − fee% − margin%)`, charm-rounded to .99;
refuse to list if profit < floor ($8 default). Then `POST /store/products` with
`sku = <EXP-ID>-<variant>` — that SKU is the fallback attribution link from a
Square order back to the experiment. Poll Square's catalog until the item and
its variations appear, map variation ids into `exp_variations`. Gate: both
systems must agree the item exists **and is purchasable**. Then CatalogBot
verifies the live storefront (Constraint 1) and records the result; three
invisible listings in a row is an ops alert, because that means the integration
is broken, not that three shirts are shy.

**Stage 6 — TESTING.** Drive traffic. Organic first (post the mockups — free,
slow, honest), then paid at a capped daily budget ($8–10) via Square Marketing
or Meta. Record impressions/clicks from the platform — the maturity gate needs
them, and without them nothing ever becomes decidable. Sales arrive via Square
webhooks: `order.updated` → line item `catalog_variation_id` → experiment →
`sales` row. Metrics are **recomputed from the sales table**, never incremented
(Square retries webhooks; incrementing drifts, recomputing converges).

**Stage 7 — verdict.** Mature = **≥5 days AND ≥800 impressions**. Then: CTR
≥0.8%, ≥1 order, CVR ≥0.8%, ROAS ≥1.5 if paid, refunds ≤8%. Pass ⇒ SCALING
(raise price first — highest-leverage, zero-cost test). Fail ⇒ KILLED **and
delisted from both systems** — a killed design left live keeps taking orders you
decided you didn't want. Immature ⇒ held. Killing on 40 impressions isn't a
decision, it's noise.

### Phase 6 · Guardrails and the kill switch

Pre-action gate on every mutating call, plus a 15-minute routine:

| Threshold | Default | Effect |
|---|---|---|
| IP strikes | **1** | hard halt, human review required |
| weekly fulfillment spend | $250 | hard halt (catches retry loops & self-purchases) |
| daily / weekly ad spend | $10 / $70 | hard halt |
| new listings / day | 6 | pause |
| concurrent tests | 10 | pause |
| refund rate (30-day, n≥10) | 8% | hard halt |
| Printful `order_failed` in 48h | 3 | hard halt |
| orders in NEEDS_APPROVAL | **1** | hard halt |

And a manual kill switch you should bookmark before you need it:

```bash
python -m pod.cli halt "reason goes in the audit log"        # stop everything
python -m pod.cli halt "IP notice received" --delist-all     # nuke all listings
python -m pod.cli resume "reviewed the notice, cleared"      # human-only restart
```

HALTED experiments never auto-resume. That is deliberate: they were halted for
a reason, and reasons need a person.

**Note on the ad-spend question.** You chose fully autonomous, including paid
tests. The honest framing: automated ad spend is the one *irreversible
money-out* action in the system, so it gets two independent ceilings (Sentinel
+ policy deny on budget increases) and a hard weekly cap. If you ever want a
human in the loop anywhere, put it there — it costs you test latency, not
capability.

### Phase 7 · Launch sequence (do not skip steps)

1. `DRY_RUN=1` — every write logs its payload instead of calling out. Run
   `run-one` with 5–10 real concepts. Read the payloads. This is where you
   discover your variant ids were wrong.
2. `DRY_RUN=0`, `SQUARE_SANDBOX=1` — Square sandbox + real Printful in dry-run
   mode if you can, else sandbox end-to-end. Confirm webhooks verify (the
   signature is HMAC-SHA256 over `notification_url + raw_body`, base64'd, in
   `x-square-hmacsha256-signature`; verify the **raw** body, and the URL must
   match byte-for-byte).
3. Production, guardrails **tight**, 1–2 listings/day for the first week. Watch
   `v_kill_reasons` daily — it tells you whether your problem is demand
   (nothing scores), IP (everything rejects), or conversion (lists, never
   sells). Those are three different fixes.
4. Loosen caps only with data in hand. You can't recover money already spent.

---

## Part 3 — The money

Per unit, BC3001 black tee, Free plan:

| | |
|---|---|
| Retail | $27.99 |
| Printful base | −$13.25 |
| Shipping (avg, eaten or passed through) | −$4.50 |
| Square fees (3.3% + 30¢) | −$1.22 |
| **Contribution** | **≈ $9.02** |

Break-even on the stack at ~$40–60/month of running cost (OpenBot VPS + model
tokens + Apify + R2): **~6 shirts/month.** The machine's job is to find the
2–3 designs out of every 40 that carry the other 37. That is the real business
model: not "AI prints shirts," but **cheap, fast, disciplined experimentation at
a volume a human can't sustain** — with a state machine that kills losers before
they cost you anything and scales winners before the trend dies.

Runway math worth internalizing: at 6 designs/day you spend ~$0.10/day on
trademark checks and ~$0.30/day on image gen. The expensive line items are
fulfillment (only on real sales) and ads (capped). **The failure mode that costs
real money is not a bad design — it's a loop.** Hence the weekly fulfillment
cap.

---

## Part 4 — What will actually break (troubleshooting table)

| Symptom | Cause | Fix |
|---|---|---|
| Items exist in catalog, sell nothing | `ecom_visibility` read-only; default not set | Constraint 1 click path; CatalogBot verify step |
| Orders imported but never print | NEEDS_APPROVAL; "manually confirm" ON | Printful store settings; Sentinel halts at ≥1 |
| Everything stops suddenly | Billing method missing/expired | Printful → Billing |
| Mockups take forever / 429 storms | 2 req/min new-store limit, aggressive retry | let the client's bucket pace it; don't add retries |
| Designs render with garbage letters | art prompt contained text | `design.py` clause check; phrase goes to Pillow |
| Sales not attributed to experiments | variation mapping lost | SKU carries exp id as fallback; check `exp_variations` |
| Webhooks all fail signature | URL mismatch or JSON re-serialization | match `PUBLIC_WEBHOOK_URL` byte-for-byte; verify raw body |
| Same shirt rebuilt daily | dedupe index missing | `experiments_concept_key` unique index |
| Bot does something alarming | policy still the permissive shipped default | replace with `openbot/policy.json`; check `/admin/audit` |
| Test stuck in TESTING forever | no impressions recorded | `record_traffic` from the ad platform is mandatory |

---

## Part 5 — Honest caveats

- **OpenBot is alpha.** No-auth default, open authorization issues, moving YAML
  and policy schemas. Self-host behind a tailnet, pin the commit, read
  `/admin/audit` weekly. The governance model is the reason to use it; the
  maturity is the reason to keep the caps tight.
- **Unofficial endpoints are unofficial.** The Google Trends widget API can
  change without notice; Etsy scraping violates their ToS (use Open API v3 in
  production); Amazon will captcha a datacenter IP (hence the browser step).
  Every source failure must read as "no signal," never as "no demand" — the code
  is written that way on purpose, and your reports should say the same.
- **A LOW IP verdict is not legal advice.** It means no live Class 25 conflict
  in the registries checked. It does not cover common-law marks, artwork
  copyright, or jurisdictions you didn't check. The reverse-image layer is
  stubbed and fails closed until you wire it.
- **The benchmarks are priors.** CTR/CVR thresholds, score weights, the 5-day
  maturity window: reasonable starting points, not truths. Refit from your own
  data — `v_kill_reasons` and `v_funnel` exist precisely so you can.
