# POD Autopilot — the $0 plan

*Rewrite of BLUEPRINT.md + RUNBOOK.md for one constraint: **no cash**. Inputs you
have: Square + Printful accounts, Claude Pro, ChatGPT Pro, GitHub Education
(Student Developer Pack), a laptop, time.*

---

## 0. The short version

Same machine, free plumbing. Nothing about *what* it does changes: discover
demand from public signals → score → trademark-screen → design → mock up →
list on Square via Printful → test with real strangers → scale winners, kill
losers → guardrails that can stop the whole thing. What changes is *where it
runs and who it pays*:

| In the paid plan | Cost | In this plan | Cost |
|---|---|---|---|
| VPS running OpenBot + Postgres 24/7 | $6/mo + ops | **GitHub Actions cron** (Pro via Student Pack: 3,000 min/mo) | $0 |
| Bot model tokens (API) | $25–80/mo | **Claude Code on your Claude Pro** (`claude setup-token`, officially supported in Actions) → fallback **Gemini free tier** → fallback **GitHub Models** | $0 |
| gpt-image-1 art | ~$30/mo | **LLM writes SVG, we rasterize it** at 4500×5400 (sharper than any upscaled raster); optional **Cloudflare Workers AI FLUX** (10k free neurons/day) | $0 |
| Real-ESRGAN upscaling | free but a GPU helps | **Not needed** for vector art | $0 |
| Cloudflare R2 for print files | ~$1 | **Public GitHub repo release assets**, auto-deleted after Printful ingests | $0 |
| Public webhook URL + tunnel + TLS | $1–2 + ops | **Poll Square/Printful every 30 min** from Actions. Nothing listens. | $0 |
| Postgres on the VPS | — | **Neon free tier** (no card) | $0 |
| Apify USPTO checks | $3–5 | Apify **free plan $5 credits/mo** ≈ 1,000 checks (we need ~120) | $0 |
| Paid ad tests ($70/wk cap) | ≤$280/mo | **Organic test**: Pinterest auto-publish from an RSS feed + a free click-counting Worker; longer windows, clicks+orders instead of impressions | $0 |
| CopilotKit license | $0 (Developer) | OpenBot becomes **optional** (see §9) | $0 |
| **Fixed monthly** | **$40–120 + ads** | | **$0** |

Break-even at $0 fixed cost is **zero shirts**. Every sale is ~$9 profit from
day one. The price you pay instead is *time*: organic tests take 2–5 weeks to
read, not 5 days, and Pinterest traffic ramps slowly.

**The one thing that is not literally $0:** Printful charges *you* for each
order when it imports (≈$13–18 for a tee), and Square pays *you* out the next
business day. That's ~1 day of float per order. You need a card or PayPal on
Printful's billing page. If a charge fails, Printful holds the order
(nothing is lost; it ships when paid) — but a held order is a slow order, so
keep one card there even if it's near-empty, and the Square payouts refill it.

---

## 1. What each of your three subscriptions does

### Claude Pro → the runtime brain
Claude Code (`claude` CLI) is included in Pro. Run `claude setup-token` once
on your laptop; it prints a long-lived OAuth token you store as the GitHub
secret `CLAUDE_CODE_OAUTH_TOKEN`. Anthropic documents this path for Pro/Max
users in CI (`claude_code_oauth_token` input on the official action). Our
`pod.llm` module calls `claude -p` headless with **no tools**, so each call
is a single completion. Budget: the pipeline makes ~3–6 LLM calls per day
(1 discover, 1 SVG per design × up to 4, 1 weekly review). That fits a Pro
5-hour window with room to spare.

Caveat (be honest with yourself): subscription OAuth tokens have had
periods where third-party/CI use was rejected. That's why `LLM_PROVIDER` is
a chain: `claude_code,gemini,github_models`. If Claude's lane fails, Gemini
takes the call and nothing stops.

### ChatGPT Pro → the builder and the mechanic
Codex (cloud + CLI) is included in Pro with generous limits. Use it where a
human would otherwise sit with the code:
* **Setup day**: "read docs/ZERO_CASH_PLAN.md and help me fill every secret" —
  it walks you through each dashboard.
* **Self-repair**: when the `ops` workflow opens a `halt` issue, paste the run
  log into Codex and let it patch and PR. (If you kept Copilot from the
  Student Pack, you can assign the issue to `@copilot` instead; the workflow
  text invites that.)
* **Manual design boost (optional, not required)**: ChatGPT image generation
  is unlimited in the app. Once a week you *could* paste 5 art prompts from
  `state/candidates.json`, drop the PNGs in `inbox/`, and they get used in
  preference to SVG. You chose fully autonomous, so this is off by default.
  I mention it because it's free quality you already own.

`codex exec` also works headless **on your laptop** (not in Actions — it
needs your interactive login). `LLM_PROVIDER=codex` is wired for that case.

### GitHub Education → the server you don't have
* **GitHub Pro**: 3,000 Actions minutes/mo on private repos (we use ~500),
  **Pages on a private repo** (serves the Pinterest feed), Codespaces hours
  for a dev box.
* **Azure for Students, $100, no card**: the escape hatch if you ever want a
  real 24/7 VM (for OpenBot, see §9). Not needed for this plan.
* **DigitalOcean $200 for a year**: needs a $5 hold on a card/PayPal, so it
  doesn't fit "no cash" today; bank it for later.
* **Namecheap .me / .tech domain**: optional vanity for the click-counter.
  square.site subdomain is fine for the store.
* **Copilot Pro** (if you got it before the April 2026 sign-up pause): GitHub
  Models limits are higher, and the coding agent can take self-repair issues.

---

## 2. Architecture (zero-cash)

```
 ┌──────────────────────────── GitHub (private repo, Pro) ─────────────────────────────┐
 │  .github/workflows/                                                                  │
 │   daily-discover-design  07:17 PT   raw signals → LLM → candidates → score → IP →    │
 │                                     SVG design → mockups → Printful sync → Square     │
 │   ops                    */30 min   poll Square orders/refunds + Printful status,     │
 │                                     pull clicks, decide tests, Sentinel, rebuild feed │
 │   weekly-review          Sun        LLM writes REPORT.md → GitHub issue               │
 │   pages                  on push    serves public/feed.xml (Pinterest RSS)            │
 │  secrets: CLAUDE_CODE_OAUTH_TOKEN GEMINI_API_KEY PRINTFUL SQUARE APIFY DATABASE_URL   │
 └───────┬───────────────┬────────────────┬──────────────────┬──────────────────────────┘
         │               │                │                  │
   Neon Postgres    pod-assets repo   Cloudflare (free)   SaaS you own
   (free, no card)  (PUBLIC; print    ├ Worker  /go/<id> → 302 → Square, counts clicks
   experiments,     files live here   │         /stats  → clicks per experiment
   sales, spend,    ~48h, then        └ Workers AI (optional FLUX art)
   events           auto-deleted)
                                        Square Online (Free plan, 3.3%+30¢)
                                        Printful (fulfils, pushes tracking)
                                        Pinterest Business (auto-publish from RSS)
                                        Apify free ($5/mo → USPTO checks)
```

Steady state: **zero processes you run**. GitHub's scheduler is the daemon.
Your laptop is only for setup and for reading the weekly issue.

Trust model is unchanged from the paid plan: the LLM proposes JSON; Python
validates demand with real HTTP calls, screens trademarks (fails closed),
builds the print file, enforces every cap. Nothing an LLM says can move
money. There *is* no money to move anyway: the only outbound spend in the
whole system is Printful fulfilment, which only happens after a customer
has paid.

---

## 3. Play-by-play

Everything below is clicks and commands. Each item says "free" so you can
see no card is being asked for. Total: one evening plus a second short
session when Pinterest claim verification lands.

### Day 1 — accounts and keys (all free)

1. **Claude Pro token** (laptop)
   ```bash
   npm i -g @anthropic-ai/claude-code
   claude            # log in with your Pro account once
   claude setup-token    # copy the token → GitHub secret CLAUDE_CODE_OAUTH_TOKEN
   ```
2. **Gemini free key**: aistudio.google.com → *Get API key* → secret `GEMINI_API_KEY`. No billing account. Free tier is hundreds of requests/day on 2.5 Flash; we use <20.
3. **GitHub Models** (fallback #2): github.com → Settings → Developer settings → Fine-grained PAT → *Account permissions → Models: Read* → secret `GH_MODELS_TOKEN`.
4. **Neon** (neon.tech): sign in with GitHub → new project `pod` → copy the pooled connection string → secret `DATABASE_URL`. Then once, locally:
   ```bash
   psql "$DATABASE_URL" -f sql/schema.sql
   ```
   (Supabase free works too; Neon's "no card, suspends when idle" fits a cron workload perfectly.)
5. **Apify** (apify.com): free plan, no card → Settings → Integrations → token → secret `APIFY_TOKEN`. $5/mo credit ≈ 1,000 checks; 4 designs/day × 30 = 120.
6. **Printful**: Settings → API → token → secret `PRINTFUL_API_TOKEN`. Also, now, the four silent-failure switches:
   * Settings → Orders → *Manually confirm imported orders* **OFF**
   * Billing → a payment method present (the float, see §0)
   * Stores → your Square store is connected
   * Settings → Stores → *Product sync* on
7. **Square**: developer.squareup.com → your app → **Production** access token (scopes `ITEMS_WRITE ITEMS_READ ORDERS_READ PAYMENTS_READ MERCHANT_PROFILE_READ`) → secrets `SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID`. Variable `SQUARE_STORE_URL=https://<you>.square.site`. Dashboard switches:
   * Online → Items → Item Sync → **visibility default: Visible**
   * Online → Settings → Square Sync → *Mark newly imported items as unavailable online* **OFF**
   (These two replace the browser-flip the paid plan did with a Bot. `ecom_visibility` is read-only via API, so the default is the automation.)
8. **Assets repo**: create a **public** repo `pod-assets` (empty README). Fine-grained PAT, *Repository: pod-assets, Contents: Read and write* → secret `ASSETS_GH_TOKEN`; variable `ASSETS_GH_REPO=<you>/pod-assets`. Print files sit here ≤48h as release assets and are deleted by the ops run.

### Day 2 — the repo, dry run in the cloud

```bash
git init && git add -A && git commit -m "pod-autopilot zero-cash"
gh repo create pod-autopilot --private --source . --push
```
Repo → Settings → Secrets and variables → Actions: paste the secrets above.
Variables: `DRY_RUN=1`, `ASSETS_GH_REPO`, `SQUARE_STORE_URL`, `POD_NICHES`.
Settings → Pages → *Source: GitHub Actions*.

Then: Actions → `daily-discover-design` → *Run workflow*. Watch the log:
`zero_cash_check` must print `0 problem(s)`; `discover` prints candidates with
evidence lines; `design` prints a trace like
`scored=0.63 | ip=clear | design=built | mockups=5 | listed @ $27.99 (profit $9.02) | testing=started`
with `[DRY_RUN]` on every write. The SVG masters land in the run's artifacts;
download one and look at it at 100% — this is the art the LLM lane makes.

Run `ops` manually once too; it should finish green with an empty summary.

### Day 3 — $0 traffic plumbing

1. **Click counter** (Cloudflare free, no card):
   ```bash
   npm i -g wrangler && wrangler login
   cd worker/click-counter
   wrangler kv namespace create CLICKS     # paste id into wrangler.toml
   wrangler secret put STATS_TOKEN         # openssl rand -hex 24
   wrangler deploy                         # → https://pod-go.<you>.workers.dev
   ```
   Variable `CLICK_WORKER_URL`, secret `CLICK_STATS_TOKEN`.
2. **Feed**: after the first `ops` push, `https://<you>.github.io/pod-autopilot/feed.xml` exists. Variable `FEED_SITE_URL` = that site root.
3. **Pinterest Business** (free): create/convert account → Settings → *Claimed accounts* → claim the GitHub Pages site (drop the HTML verification file in `public/`, push) → *Create → Bulk create Pins → Auto-publish* → paste the feed URL, pick a board ("Nurse Tees" etc.; one board per niche is better for distribution). Pinterest polls it ~daily and creates a Pin per new listing with the tracked link.

   Don't automate Reddit/TikTok/IG. Account bans are a real cost, and the
   pipeline's value is that nothing it does can get your personal accounts
   nuked.

### Day 4 — go live

Change **one** repo variable: `DRY_RUN=0`. That's the switch. Then run
`daily-discover-design` by hand and verify the proof chain, same as the
paid RUNBOOK: Printful → Products shows the sync product with mockups;
Square → Items shows it; the public square.site page shows it *visible*
with the right price; `ops` registered the link (Worker `/stats` returns
`{}` with your token = auth works). Then leave it alone.

### Weeks 1–5 — what "working" looks like at $0

* Week 1: 15–25 listings. Pinterest pins appear 1–2 days after listing. Clicks
  trickle: 0–5/day total. Zero sales is normal.
* Week 2–3: pins start ranking for long-tail searches. Tests reach 14 days
  but most won't have 40 clicks yet, so they're held, not killed.
* Week 4–5: first batch hits `ORGANIC_MAX_DAYS=35` → decided regardless. Expect
  most to be killed on `orders`. 1–3 of 30 designs with any sale is a
  realistic organic hit rate for a new store. That's the data the next
  month's discovery feeds on (the `review` job tells you which niches to drop).
* Guardrail reality: `CAP_CONCURRENT_TESTS=12` and 4 listings/day means you
  hit the cap in 3 days and the pipeline idles until kills free slots. That
  is correct behaviour; raise the cap only when the kill cadence is healthy.

---

## 4. The design engine without an image model

`ART_PROVIDER` picks the lane. All three ship; default is `svg`.

**`svg` (default).** The LLM is asked for pure-geometry SVG (viewBox 0 0 1000
1000; path/circle/rect/polygon/gradients only; **no text elements**, no
images, no external refs, transparent canvas). `design_svg.sanitize_svg`
parses it, rejects anything off that whitelist, strips any full-bleed
background rect, and `rasterize` renders it with cairosvg at the exact
pixel width the canvas wants (3,240 px = 72% of 4,500). Pillow then typesets
the screened phrase in Anton (OFL) exactly as before. Result: a 4500×5400
300-DPI transparent PNG around 0.2–0.5 MB — sharper than the diffusion +
Real-ESRGAN route, because there is no resampling at all. The SVG is kept
as a sidecar: re-rendering a winner for a mug or hoodie is a resize.

This is a good match for what sells: badges, silhouettes, retro sun stripes,
line icons, paw prints, EKG lines, mountains. It cannot do painterly or
photoreal — which also prints worst on DTG, so you lose little.

**`none`.** Typography-only. Largest POD category by volume, zero LLM art
calls. Good when Claude's window is exhausted; `discover` can mark a
candidate `layout: stacked` with no `art_subject`.

**`cloudflare`.** FLUX.1-schnell on Workers AI, free 10k neurons/day (one
image ≈ $0.0006 worth, so ~100+/day). 1024 px output, so `UPSCALER_CMD`
(Real-ESRGAN, runs on the Actions CPU, ~1–2 min) applies. Opt in per niche
when you want texture.

The IP gate is unchanged: phrase + subphrase are screened against the
blocklist, regex patterns, and USPTO live/pending marks via Apify; it fails
closed on any error. Vector art reduces IP risk further — there's no
diffusion model quietly reproducing a logo it saw in training.

---

## 5. Testing without ads

No impressions exist, so the paid gate (CTR ≥0.8%, 800 impressions, 5 days,
ROAS) can't apply. `TRAFFIC_MODE=organic` swaps in:

| | Value | Env |
|---|---|---|
| Mature when | ≥14 days **and** ≥40 tracked clicks, **or** ≥35 days regardless | `ORGANIC_MIN_DAYS`, `ORGANIC_MIN_CLICKS`, `ORGANIC_MAX_DAYS` |
| Kill | mature and 0 orders | — |
| Kill | ≥40 clicks and orders/clicks < 1% | `ORGANIC_MIN_CVR` |
| Kill (any time) | refund rate > 8% | `CAP_REFUND_RATE` |
| Scale | mature, ≥1 order, CVR ≥1% | — |

Clicks come from the Worker (bots and link-preview fetchers filtered by
UA). Orders come from Square polling, attributed by `catalog_variation_id`.
Both are idempotent, so the 72-hour re-read window on every ops run is
harmless. "Scale" at $0 means: keep it listed, it gets the top slot in the
feed, and `discover` is told to make siblings (same niche, new phrase).

These are priors. After ~30 decided tests, run the retune SQL in RUNBOOK
Part 8 and set the four env vars from your own numbers.

---

## 6. Free-quota budget (what actually limits you)

| Resource | Free allowance | Daily use at 4 listings/day | Headroom |
|---|---|---|---|
| GitHub Actions minutes | 3,000/mo (Pro) | ~15 (daily) + 48×1.5 (ops) ≈ 90/day ≈ 2,700/mo | tight — see note |
| Claude Pro (Claude Code) | per 5-hour window | 1 discover + ≤4 SVG + weekly review | fine |
| Gemini 2.5 Flash | hundreds RPD | fallback only | fine |
| GitHub Models | ~50 RPD big models | fallback only | fine |
| Apify | $5/mo | 4 checks | 8× |
| Printful mockup generator | 2 req/min (new store) | 4 designs × 1 task | fine; `CAP_NEW_LISTINGS_DAY` keeps it so |
| Cloudflare Worker | 100k req/day | clicks | effectively unlimited |
| Workers AI | 10k neurons/day | 0 unless `cloudflare` lane | fine |
| Neon | 0.5 GB, 100 CU-h/mo | KBs; wakes per cron | fine |
| GitHub Pages | 1 GB, 100 GB/mo | one XML file | fine |

Note on minutes: `ops` every 30 min is the big consumer. If you get close to
3,000, change its cron to `*/60`. Latency on a 5-week test is irrelevant.
(Public repos get unlimited minutes, but the pipeline repo holds your
candidates and kill data — keep it private; only `pod-assets` is public.)

---

## 7. Economics, unchanged math, zero fixed cost

Square Online **Free** plan: 3.3% + 30¢ per online sale. Printful BC3001
black tee base ≈ $13.25 + ~$4.50 shipping. Formula in `config.Economics`:
`retail = (base + shipping + 0.30) / (1 − 0.033 − 0.30 margin)` → $27.99 charm →
**≈ $9.02 contribution per shirt**. Fixed costs $0 → **break-even at 0 units**.
Cash-flow: Printful charges at import; Square pays out next business day;
you carry ≈$18 for ≈1 day per order. If a charge bounces, Printful holds the
order until paid — not lost, just slow. When you have your first $30 of
profit, leave it on the Printful billing card and the float problem is gone.

When cash *does* exist, the first dollars that buy the most, in order:
1. Nothing until 10 real sales. Data first.
2. Pinterest/Meta ads at $5/day on the **already-proven** winners (flip
   `TRAFFIC_MODE=paid` for those via the `test_started` budget). This is the
   paid plan's ad lane, but applied only to designs organic already validated.
3. Gemini paid Tier 1 (no minimum) to stop depending on the Claude token.

---

## 8. Guardrails and honest caveats for this mode

* **Caps**: `CAP_WEEKLY_ADS=0`, `CAP_DAILY_ADS=0` are enforced and
  `zero_cash_check` refuses to start if `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`
  are present. A mis-set provider cannot create a bill.
* **Sentinel** semantics are identical: IP strike ⇒ HALT, refund rate ⇒ HALT,
  fulfilment cap ⇒ HALT. A HALT fails the `ops` run, which opens a GitHub issue
  labelled `halt` — that's your pager. `python -m pod.cli resume "note"` is
  still human-only.
* **Cron drift**: GitHub schedules can run 5–20 min late under load and are
  skipped if the repo has no pushes for 60 days. `ops` pushes `feed.xml`, so
  the repo stays active.
* **Token expiry**: Claude's OAuth token and fine-grained PATs expire (PATs
  up to 1 year). The chain degrades to Gemini; the readiness step warns.
  Put a yearly calendar reminder.
* **Public assets repo**: print files are briefly public. They're public on
  the product page anyway; the SVG master stays private.
* **Pinterest**: auto-publish is a legit, supported feature, but Pinterest
  still rate-limits bulk creation and dislikes identical descriptions. The
  feed uses per-listing titles, niche hashtags, and ≤4 listings/day. Don't
  raise `CAP_NEW_LISTINGS_DAY` above ~8 for the feed's sake.
* **Square product URLs** aren't exposed by API, so tracked links resolve
  via `square.site/s/search?q=<title>`; it lands on a one-result page. If
  you later move to a custom domain you can hard-code slugs.
* **Reverse-image IP layer** is still a stub that fails closed — same as the
  paid plan. Vector art makes it matter less, not zero.
* **Printful variant ids** in `pipeline.DEFAULT_VARIANTS_BLACK` must still be
  verified once against live `GET /products/71`.

---

## 9. Where OpenBot stands now

OpenBot (CopilotKit) was the orchestrator and browser agent in the paid
plan. Its two costs were a 24/7 host and LLM tokens. In this plan GitHub
Actions is the orchestrator and there are no browser steps (the Square
visibility default replaced the only one that mattered), so **OpenBot is
optional**. The `openbot/` folder, policy and MCP tool server are untouched
and still work; two ways to use them for free:

* **Cockpit mode (recommended if you want it):** run `docker compose up` on
  your laptop when you want to *talk* to the pipeline ("why was exp_… killed?",
  "show me this week's funnel"). CopilotKit Developer licence is free; point
  its model at Gemini free or your Claude token. Shut it when done. Routines
  stay in Actions.
* **Always-on:** Azure for Students ($100, no card) B2s VM runs the stack
  for ~3 months on the credit. Pointless for now; useful later if you add
  browser-only sources (Amazon BSR).

Either way the YAML tenant / CEL policy caveats from the paid plan still
apply (alpha software, reconcile field names against the pinned commit).

---

## 10. What changed in the repo

| Path | Role |
|---|---|
| `src/pod/llm.py` | provider chain: claude_code / codex / gemini / github_models; never API keys |
| `src/pod/design_svg.py` | SVG whitelist sanitizer + cairosvg rasterizer + demo art |
| `src/pod/imagegen_cf.py` | optional FLUX on Workers AI |
| `src/pod/design.py` | `ART_PROVIDER` dispatch; alpha-aware compositing; typography-only lane; GitHub-release upload |
| `src/pod/assets_github.py` | public release assets with TTL cleanup |
| `src/pod/poll_orders.py` | replaces the webhook server (Square orders/refunds, Printful status/spend) |
| `src/pod/traffic.py` | Worker link registry + click sync + Pinterest RSS |
| `src/pod/batch.py` | `discover / design / ops / review` — the routines, as cron jobs |
| `src/pod/config.py`, `state.py` | `TRAFFIC_MODE=organic` gate + maturity rules (88 existing checks unchanged) |
| `worker/click-counter/` | Cloudflare Worker + wrangler.toml |
| `.github/workflows/*.yml` | the scheduler |
| `scripts/zero_cash_check.py` | readiness gate; refuses paid keys |
| `.env.zero.example`, `examples/candidates.demo.json`, `Makefile` targets `zero-check zero-demo discover design ops review worker` | |

Offline proof, no keys: `make validate` (88/88) and `make zero-demo`, which
renders the bundled vector art at print size and lists the demo tee in
DRY_RUN with no warnings.
