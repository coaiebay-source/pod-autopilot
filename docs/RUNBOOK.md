# RUNBOOK — how to actually build and run this, in order

This is the "go deeper" document. `BLUEPRINT.md` explains *what* the system is
and *why*; this one is the literal sequence of commands, clicks, checks, and
decisions from empty machine to autonomous store — plus the operating rhythm
for after it's live.

Time budget if you're focused: **~1 week part-time** to first live listing,
then 15 min/day + 1 hr/week to operate.

---

## Part 0 — Answering "do I download the files and run the .py?"

Not quite. There are **five separate things**, and understanding the split
prevents 90% of setup confusion:

| # | Thing | Where it runs | What it is |
|---|---|---|---|
| 1 | **This folder** (`pod-autopilot/`) | your machine/VPS | the pipeline: Python library + CLI + MCP server + webhook receiver. You *install* it (`pip install -e .`), you don't "run the .py" — you run its commands (`make validate`, `make demo`, `make webhooks`…) |
| 2 | **OpenBot** | same machine, in Docker | a *separate* open-source project (github.com/CopilotKit/OpenBot). You clone or `docker run` it. It hosts the five Bots, the policy gateway, the audit log, the schedules |
| 3 | **Postgres** | same machine | OpenBot brings one (embedded or compose). The pipeline needs its own small database (`make schema`) |
| 4 | **Three SaaS accounts** | cloud | Square (storefront+money), Printful (printing), plus small utilities: Cloudflare (R2 for hosting design PNGs + Tunnel for webhooks), Apify (USPTO lookups), an LLM key |
| 5 | **A public HTTPS URL** | cloud | for webhooks. Square will not POST to http or to your laptop's localhost |

Processes that stay running in steady state:

```
OpenBot server + supervisor + bot containers   (Docker, port 3001 + internal)
pod webhook receiver                           (uvicorn, port 8080, behind TLS)
Postgres                                       (5432/5433)
(MCP server: launched BY OpenBot as a stdio child — not a daemon)
```

Everything else (`validate`, `demo`, `run-one`, `report`, `halt`) is
one-shot CLI. **The Bots invoke the pipeline; you mostly don't.**

---

## Part 1 — Shopping list

### Hardware
One always-on Linux box with 4 GB RAM / 2 CPU / 40 GB. Options:
- $5–6/mo VPS (Hetzner CX22, DO Basic, Oracle free-tier ARM if you can get it)
- an old laptop/desktop at home (free; pair with Cloudflare Tunnel so webhooks
  reach it without port-forwarding)

Docker + ~6 GB disk for images. The design engine is CPU-only except the
upscaler (Real-ESRGAN ncnn runs acceptably on CPU: a few seconds per image).

### Accounts & keys (create in this order)

| Account | What you grab | Cost |
|---|---|---|
| Square | seller account + Online site published; Developer app → **Personal Access Token** (scopes: `ITEMS_WRITE ITEMS_READ ORDERS_READ PAYMENTS_READ MERCHANT_PROFILE_READ`); webhook **signature key** | free plan: 3.3% + 30¢/online sale |
| Printful | account; store token (*Settings → API*); Square store connected (*Stores → Choose platform → Square*) | $0 monthly |
| Cloudflare | R2 bucket (public read, custom domain) + Tunnel (`cloudflared`) | ~$0–1 |
| OpenAI | API key (image gen; model tokens if BOT_PROVIDER=openai) | usage |
| Anthropic | API key for the Bots (BOT_PROVIDER=anthropic) | usage |
| Apify | token (USPTO TESS actor) | ~$3–5/mo at 6 designs/day |
| Etsy | Open API v3 key (optional but recommended for competition data) | free, needs approval |
| CopilotKit | Intelligence account → license token for OpenBot | **Developer plan: free forever** (1 seat, 200 threads, 3-day retention). Pro if you want longer thread memory. The $600/mo self-hosted-K8s tier is for companies, not you |

### Realistic monthly cost sheet

| Line | Est. |
|---|---|
| VPS | $6 (or $0 home box) |
| Domain + R2 + Tunnel | $1–2 |
| CopilotKit Intelligence | $0 (Developer) |
| Bot model tokens (5 bots, daily routines) | $25–80 |
| Image gen: 6 designs/day × high tier ($0.167) | ~$30 (medium tier ≈ $8 if you accept softer art; high is right for print) |
| Apify USPTO | $3–5 |
| **Fixed total** | **~$40–120/mo** |
| Ads (capped by Sentinel) | ≤ $70/wk, i.e. ≤ $280/mo *worst case*, usually far less |
| Fulfillment | only on real sales (~$13–18/shirt, recovered in price) |

Break-even at $9.02 contribution/shirt and $80 fixed ≈ **9 shirts/month** — one
moderately successful scaling design covers the whole machine. That's the
entire business case in one sentence.

---

## Part 2 — Day 1: the pipeline on your machine (~40 min)

```bash
# 1. get the folder onto the machine (download the workspace zip, scp, git — anything)
cd pod-autopilot

# 2. install
make install                       # pip install -e ".[dev]"

# 3. the safety suite. Expect: 88/88 passed. If not, STOP and fix.
make validate

# 4. the offline rehearsal of the whole chain. Expect the trace line:
make demo
#   "trace": "scored=0.78 | ip=clear | design=built | mockups=5 |
#             listed @ $27.99 (profit $9.02) | testing=started"

# 5. readiness check in dry mode. Expect: only ASSET_BASE_URL-ish WARNs and the
#    manual checklist. Every FAIL must be fixed before live.
make check
```

What each demo trace element proves:
- `scored=0.78` — synthetic here (flagged in the log as SYNTHETIC); live, this
  number comes from real lookups or the concept dies.
- `ip=clear` — blocklist + (live) USPTO ran and passed.
- `design=built` — a real 4500×5400 @300 DPI transparent PNG exists in
  `artifacts/`. Go look at one. That's your product.
- `mockups=5` — Printful task flow works (fixture in dry-run; real in live).
- `listed @ $27.99 (profit $9.02)` — pricing math from live base cost + fees.
- `testing=started` — state machine reached TESTING; attribution is wired.

### Fonts and the upscaler (quality gates people skip)

```bash
# font (repo already ships Anton — OFL, commercial embedding OK)
ls fonts/    # Anton-Regular.ttf

# super-resolution: generated art is 1024px; the print canvas wants ~3240px.
# Naive 3.2x upscale = ~95 effective DPI = soft print. Fix:
wget https://github.com/nihui/realesrgan-ncnn-vulkan/releases/latest/download/realesrgan-ncnn-vulkan-ubuntu.zip
unzip realesrgan-ncnn-vulkan-ubuntu.zip && chmod +x realesrgan-ncnn-vulkan
export UPSCALER_CMD="realesrgan-ncnn-vulkan -i {in} -o {out} -s 4 -n realesrgan-x4plus"
```

Put `UPSCALER_CMD` in your `.env`/shell profile. Without it every design logs a
`art_upscaled_3.2x_without_superres` warning — visible in reports, and your
first customer photo will show you why it matters.

---

## Part 3 — Day 1: database + assets (~20 min)

```bash
sudo -u postgres createdb pod
# or on your VPS:  createdb pod
export DATABASE_URL=postgresql://$USER@localhost:5432/pod   # or a dedicated role
make schema
psql "$DATABASE_URL" -c "select * from v_funnel;"   # empty table = good
```

R2 bucket:
1. Cloudflare → R2 → Create bucket `pod-assets`.
2. Settings → connect a custom domain `assets.yourstore.com` (public read).
3. `export ASSET_BASE_URL=https://assets.yourstore.com`
4. Sync strategy: simplest is `rclone bisync`/cron from `ASSET_DIR`, or swap
   `design.upload_asset()` for the R2 SDK (10 lines; the function is the single
   place publishing happens).

Verify: `curl -I $ASSET_BASE_URL/` should not be a 5xx.

---

## Part 4 — Day 2: the dashboard clicks, with verification (~45 min)

Do these **in order**; each has a "prove it" step. Skipping verification here is
how you get a silently dead store three weeks later.

**Square**
1. *Dashboard → Online → Items → Item Sync → Item visibility settings →*
   **Visible**. Prove: publish anything later and it appears on the site
   without touching it.
2. *Dashboard → Online → Settings → Square Sync →* confirm **"Mark newly
   imported items as unavailable online" = OFF**.
3. Publish your Square Online site; note the public URL (CatalogBot verifies
   against it).
4. Developer Dashboard → app → Personal Access Token (production) with the
   scopes above. Prove: `make check` shows "Square token valid, N location(s)".
5. Note the **webhook signature key** from the app's Webhooks page.

**Printful**
1. *Stores → Choose platform → Square → Connect → Allow.* Prove: Printful
   store list shows the Square store as connected (`make check` warns if not).
2. *Billing → Billing methods →* add card. Prove: it's listed as default.
3. *Stores → <store> → Edit → Orders →* **"Manually confirm imported orders"
   OFF**. Prove: later, `needs_approval_orders()` returns 0 on a real order.
4. *Settings → API →* token. Prove: `make check` "Printful token valid".
5. **Record your real variant ids:**
   ```bash
   curl -s -H "Authorization: Bearer $PRINTFUL_API_TOKEN" \
        https://api.printful.com/products/71 | python3 -m json.tool | less
   ```
   Write down the variant ids for Black S–2XL (and White if you'll sell it).
   Edit `DEFAULT_VARIANTS_BLACK` in `src/pod/pipeline.py` if they differ from
   the shipped values. **This is the most common first-run bug.**

**Webhooks + tunnel**
```bash
# public HTTPS into your box, no port forwarding:
cloudflared tunnel create pod
cloudflared tunnel route dns pod hooks.yourstore.com
# point the tunnel at localhost:8080 in its config, then:
make webhooks &
export PUBLIC_WEBHOOK_URL=https://hooks.yourstore.com/hooks/square
```
Register the subscription (one-time):
```bash
python3 -c "
from pod.square import create_webhook_subscription
import os
print(create_webhook_subscription(os.environ['SQUARE_ACCESS_TOKEN'],
      os.environ['PUBLIC_WEBHOOK_URL']))"
```
Prove it end-to-end with a signed test event:
```bash
python3 - <<'PY'
import hmac, hashlib, base64, json, os, requests
url = os.environ["PUBLIC_WEBHOOK_URL"]; key = os.environ["SQUARE_WEBHOOK_SIGNATURE_KEY"]
body = json.dumps({"type":"payment.updated","data":{"object":{
    "id":"ptest","status":"COMPLETED","amount_money":{"amount":100},"order_id":"otest"}}}).encode()
sig = base64.b64encode(hmac.new(key.encode(), url.encode()+body, hashlib.sha256).digest()).decode()
r = requests.post(url, data=body, headers={
    "Content-Type":"application/json", "x-square-hmacsha256-signature": sig})
print(r.status_code, r.json())   # expect: 200 {'ok': True, ...}
# and a WRONG signature must give 401:
r2 = requests.post(url, data=body, headers={
    "Content-Type":"application/json", "x-square-hmacsha256-signature": "bogus"})
print(r2.status_code)            # expect: 401
PY
```
If the good signature 401s: your `PUBLIC_WEBHOOK_URL` differs byte-for-byte from
the subscription's `notification_url` (trailing slash is the usual culprit).

Add Printful's webhook too: *Printful → Settings → Webhooks →*
`https://hooks.yourstore.com/hooks/printful/$PRINTFUL_HOOK_SECRET`, subscribe to
order/shipment events.

Keep `make webhooks` alive with systemd (or tmux while testing):
```ini
# /etc/systemd/system/pod-webhooks.service
[Unit]
Description=POD webhook receiver
After=network.target
[Service]
WorkingDirectory=/opt/pod-autopilot
EnvironmentFile=/opt/pod-autopilot/.env
ExecStart=/usr/bin/python3 -m pod.webhooks_serve
Restart=always
[Install]
WantedBy=multi-user.target
```

---

## Part 5 — Day 3: dry-run rehearsal with REAL demand data (~1 hr)

Now run the chain with **real lookups but zero writes**: `DRY_RUN=1`,
`POD_MEMORY_DB=1` (or real DB), no fake flags.

```bash
DRY_RUN=1 python3 -m pod.cli run-one \
  --niche nursing \
  --concept "Night Shift Nurse Coffee" \
  --phrase "STILL RUNNING ON CAFFEINE" \
  --keywords "night shift nurse,nurse coffee,12 hour shift" \
  --style retro_sunset \
  --art-subject "a steaming coffee cup silhouette under a starry night sky"
```

Three outcomes, all useful:
- **Killed at SCORED** ("only 1 corroborating source" / low score): *correct.*
  Your concept wasn't actually trending. Try 5–10 concepts across 2–3 niches.
  This is the anti-hallucination gate earning its keep on day one.
- **Killed at IP**: read `kill_reason`; it names the term and layer. Correct
  unless the term is obviously clean — in which case check the Apify actor
  output manually at tsdr.uspto.gov before distrusting the gate.
- **Reaches `testing=started`**: you have a real design PNG in `artifacts/`,
  real mockups aren't generated in dry-run (fixtures), and the listing payload
  was logged. Open the PNG. Judge it like a customer.

Repeat until 2–3 concepts reach the end. You are rehearsing mechanics with live
market data and zero risk. **Do not skip to live before this feels boring.**

---

## Part 6 — Day 4: OpenBot (~1.5 hr)

```bash
# Option A: published image (nothing to clone/build)
docker run -p 3001:3001 --env-file .env \
  -e EMBEDDED_POSTGRES=on -v openbot-data:/var/lib/postgresql \
  ghcr.io/copilotkit/openbot:latest

# Option B: from source (needs Bun 1.3+)
git clone https://github.com/CopilotKit/OpenBot && cd OpenBot
cp .env.example .env && bash scripts/start.sh      # UI: http://localhost:3010
```

Fill `.env` from `pod-autopilot/.env.example` (the OPENBOT section). Then, in
the UI:

1. **Replace the policy first.** Paste `openbot/policy.json` into
   `AGENT_COMPUTER_POLICY` (or set `AGENT_COMPUTER_POLICY_FILE`). The shipped
   startup policy allows *everything*; that is demo behavior. Verify field
   names against `docs/policy.md` in your pinned commit — they've moved
   between alpha releases; malformed JSON refuses to boot, which is the
   behavior you want.
2. `/settings/passwords` — sign Bots into squareup.com and printful.com
   **by hand via take-the-wheel** when each Bot first hits a login wall. That
   flow exists exactly for this and is audited.
3. `/admin/credentials` — add `PRINTFUL_API_TOKEN`, `SQUARE_ACCESS_TOKEN`,
   `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `APIFY_TOKEN`. Write-only, encrypted;
   they never enter transcripts.
4. Create the five coworkers from `openbot/tenant/pod-autopilot.yaml`
   (roles/grants/routines are all written there). If your pinned version
   doesn't load routines from the tenant package, create them in `/routines`
   or by asking in-channel — exact ask strings:
   - TrendScout: *"Every day at 13:00, run the daily demand sweep: 10 validated
     concepts, at least 3 scoring ≥0.55 with ≥2 corroborating sources, written
     to /workspace/research/ with evidence URLs."*
   - DesignDirector: *"Every day at 13:30, process the scored queue: phrase,
     style, screen IP, build if clear. Stop after 6 designs."*
   - IPGuard: *"Every day at 14:00, independently re-screen every DESIGNED or
     MOCKED_UP experiment; reject or pass; rejections kill with reason."*
   - CatalogBot: *"Every day at 14:30, publish every cleared experiment with a
     print file and ≥3 mockups, then verify storefront visibility for each."*
   - OpsSentinel: *"Every 15 minutes run pod.check_guardrails; hard_halt means
     call pod.emergency_halt immediately. Every day at 06:00 evaluate tests;
     at 07:00 send the daily report."*
5. **Smoke-test each Bot in its channel with a read-only ask** before enabling
   routines: "run pod.pipeline_status and summarize". Watch its screen
   (`/channel/:id`) — you should see the browser/container working and the
   audit rows appearing in `/admin/audit`.
6. Only then enable routines.

Grant-shape sanity check: TrendScout has browser but **no shell**; CatalogBot
has shell restricted to `python -m pod.*`; only OpsSentinel can call
`emergency_halt`. If a Bot can do something its row in the tenant YAML doesn't
grant, your policy or grants are wrong — fix before sleeping.

---

## Part 7 — Week 1: observation, Week 2: sandbox, Week 3: live

**Week 1 (routines on, DRY_RUN=1).** Read `/admin/audit` daily like a log.
You're looking for: Bots refusing to assert unvalidated trends; IP rejections
with sensible reasons; zero policy denials you didn't expect (a denial you
didn't expect = a policy rule to fix or a Bot misbehaving — both worth knowing
now, cheaply).

**Week 2 (`DRY_RUN=0`, `SQUARE_SANDBOX=1`).** Real Printful products, sandbox
Square. Verify: listings appear in sandbox catalog; mockups are real URLs you
can open; the visibility verify-step runs; webhook receiver logs sandbox
events; `v_funnel` shows movement. Also place **one manual sandbox order** on a
published item and watch: Printful imports → status flows → tracking syncs.
That single order proves the money loop before real money touches it.

**Week 3 (production, caps tight).** `DRY_RUN=0`, sandbox off, caps at
`.env.example` values (6 listings/day, $10/day ads). First week goal is **not
profit** — it's 10–20 experiments through the funnel so `v_kill_reasons` tells
you where your real bottleneck is.

---

## Part 8 — The operating rhythm

### Daily, 15 minutes
```bash
make report        # P&L, funnel, top kill reasons, guardrail state
```
Then:
1. Guardrails CLEAR? If not — read the reason, fix root cause, don't just
   resume.
2. Any NEEDS_APPROVAL or order_failed events? (report surfaces them; each is a
   paid customer waiting.)
3. Skim yesterday's new listings on your storefront **as a customer on your
   phone**. The bot's verification is mechanical; your eyes catch "this looks
   like AI garbage" before 200 more do.

### Weekly, 1 hour
```sql
-- where is the funnel leaking?
select * from v_funnel;  select * from v_kill_reasons;
-- tests approaching maturity (day 4-5): pre-read them
select id, concept, impressions, clicks, orders, round(revenue,2) rev
  from experiments where stage='TESTING'
   and listed_at < now() - interval '4 days' order by impressions desc;
-- what's actually earning
select e.concept, sum(s.qty) units, round(sum(s.gross),2) gross
  from sales s join experiments e on e.id=s.experiment_id
 group by 1 order by 3 desc limit 10;
-- spend discipline
select kind, round(sum(amount),2) from spend
 where at > now() - interval '7 days' group by 1;
```
Weekly decisions: kill/scale is automated; **your** weekly jobs are (a) review
any HALTED state, (b) look at the top 3 kill reasons and decide if a gate needs
retuning vs. your concept sourcing is bad, (c) check one scaling design's price
elasticity (raise $2 on one, watch 14 days).

### Retuning thresholds — only with data, only one at a time
- **Score threshold (0.55):** after ~50 experiments, compare median
  `demand_score` of SCALING vs KILLED. If they barely differ, your demand
  signal isn't predictive — fix sources before touching the threshold. If they
  separate cleanly, move the threshold to keep ~15–25% of concepts passing.
- **CTR/CVR gates (0.8%/0.8%):** set kill at your own mature-test median and
  scale at the top quartile. Priors exist so week 1 isn't blind; they are not
  truth.
- **Maturity (5d/800impr):** raise if your traffic is thin (decisions on noise
  kill winners); lower only if volume is high and you're capacity-bound.
- **Weights in `demand.score_idea`:** refit last, and only by comparing
  outcomes — never by vibes.

---

## Part 9 — Incident playbook

| # | Incident | First moves |
|---|---|---|
| 1 | `make report` shows BLOCKED | read reasons verbatim; each names a threshold + current value. Fix root cause (e.g. clear stuck orders), then only resume if the *condition* is gone. Never resume to "see if it works". |
| 2 | NEEDS_APPROVAL pileup | Printful → Stores → Edit → Orders → manual confirm OFF; Complete the stuck orders by hand (customers are waiting); check the product was synced at order time. |
| 3 | Listings exist but invisible | Constraint-1 path: verify the Item Sync default; CatalogBot's `record_visibility(false)` events tell you how many; three in a row = integration broken, halt publishing, investigate sync. |
| 4 | A Bot did something you didn't sanction | `/admin/audit` → filter by bot + time; the row names the rule that allowed it. Tighten policy (deny-before-allow), then re-test the exact action in-channel before re-enabling the routine. |
| 5 | Mockup 429 storms | you're exceeding 2/min (new store). Lower daily design cap; never add retries — the client already backs off; a retry loop is what caused it. |
| 6 | Sales happening but `orders` stays 0 | attribution broken: check `exp_variations` rows exist for the sold variation id; check SKUs carry the exp id; check webhook receiver logs show `order.updated` 200s. |
| 7 | Everything silent for a day | process check: is OpenBot up (`docker ps`), webhooks up (`curl localhost:8080/health`), routines not auto-disabled (10 consecutive failures disables — `/routines` shows it), model key not expired. Silence is an incident, not peace. |

And the button you hope to never press:
```bash
make halt REASON="cease-and-desist received re: design X"      # stops everything
make halt REASON="..." DELIST=1   # (or --delist-all) nuke all listings, IP emergency
make resume NOTE="legal reviewed, design X removed, cleared to resume"
```

---

## Part 10 — Honest limits, restated where they bite

- **OpenBot is alpha.** Pin the commit; keep it off the public internet; expect
  schema drift in tenant YAML/policy between releases (server refuses to boot
  on mismatch — treat that as a feature).
- **CopilotKit Developer free tier:** 200 threads, 3-day retention. Routine
  threads roll off; that's fine because **Postgres is the system of record**,
  not bot memory. If you want long conversational context with your Bots, Pro.
- **The IP screen is a screen, not counsel.** LOW ≠ legal opinion. Reverse-image
  layer is stubbed and fails closed; wire TinEye or a Lens browser-step before
  you trust artwork originality at volume.
- **Unofficial data sources are unofficial.** Trends widget API, Etsy scraping,
  Amazon BSR can break or block any week. Every failure mode in `demand.py`
  returns "no signal", never "no demand" — so a source dying shows up as
  *fewer validated concepts*, not as bad decisions. Watch for a sudden drop in
  corroboration rate; that's a source dying, not the market changing.
- **Print quality is a physical thing.** Order one sample of your first scaling
  design and wear-wash it. The pipeline can't feel fabric.

---

## Appendix A — process/port map

| Service | Port | Notes |
|---|---|---|
| OpenBot server (+UI in image) | 3001 | localhost/tailnet only |
| OpenBot app (dev mode) | 3010 | only if running from source |
| supervisor | 4500 host / 4300 container | per-Bot computers |
| agent-computer | 127.0.0.1:4100 | loopback by design |
| agent-harness (Shape B) | 4202 | only if you bring your own agent |
| pod webhook receiver | 8080 | behind Caddy/cloudflared TLS |
| pod-agent (Shape B) | 9000 | optional |
| Postgres (OpenBot / pipeline) | 5432 / 5433 | one backup covers both |
| MCP server | — | stdio child of OpenBot |

## Appendix B — change-this-file-for-that

| Want to change | Edit |
|---|---|
| pricing/margins/fees/caps | `src/pod/config.py` (+ `.env`) |
| stage gates & benchmarks | `src/pod/state.py` |
| guardrail thresholds | `src/pod/config.py` Guardrails / `.env` |
| what a Bot may do | `openbot/policy.json`, `openbot/tenant/pod-autopilot.yaml` |
| tools Bots can call | `src/pod/mcp_server.py` (+ grants in tenant YAML) |
| styles/layouts/typography | `src/pod/design.py` |
| blocklist / IP layers | `src/pod/trademark.py` |
| demand sources & weights | `src/pod/demand.py` |
| niche list & subreddits | `demand.NICHE_SUBREDDITS`, TrendScout role |
| product/variant ids | `src/pod/pipeline.py` top constants |
