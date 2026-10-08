# START HERE — do it tonight, step by step

Plain-English, in order. Each step says **where to go → what to click → what to
copy → where to paste → how you know it worked.** Nothing here asks for a card.
If anything errors, paste the exact error into Codex or Claude and say
"I'm on step X of START_HERE.md". Budget: about 2–3 hours total, in 12 parts.

Keep a scratch note open. You'll collect about 12 values ("secrets") and
paste them all into GitHub in Part 8. Never put them in a file you commit.

---

## PART 1 — Your laptop (15 min)

**1.1 Install the four tools** (skip any you already have):
- **Python 3.12** → python.org/downloads (tick "Add to PATH" on Windows)
- **Node.js 22 LTS** → nodejs.org
- **Git** → git-scm.com
- **GitHub CLI** → cli.github.com

Open a terminal (Mac: Terminal; Windows: PowerShell) and check:
```bash
python3 --version   # (Windows: python --version)  → 3.12.x
node --version      # → v22.x
git --version
gh --version
```

**1.2 Get the project folder.** Download `pod-autopilot.zip` from this
workspace, unzip it somewhere simple, e.g. `~/pod-autopilot`. Then:
```bash
cd ~/pod-autopilot
pip install -e . "psycopg[binary]" cairosvg
make validate      # expect: 88/88 checks passed
make zero-demo     # expect a line ending in: listed @ $27.99 (profit $9.02) | testing=started
```
(Windows without `make`: run the two commands inside the Makefile targets
by hand, or install make via `winget install GnuWin32.Make`.)

✅ You just ran the entire pipeline offline with fake data. Everything after
this is plugging real accounts into it.

---

## PART 2 — GitHub: the repo that will run everything (10 min)

**2.1 Log in and push the private repo**
```bash
gh auth login            # choose GitHub.com → HTTPS → login with browser
cd ~/pod-autopilot
git init && git add -A && git commit -m "pod-autopilot"
gh repo create pod-autopilot --private --source . --push
```
✅ github.com/YOU/pod-autopilot exists and shows your files.

**2.2 Create the PUBLIC assets repo** (print files sit here for 48 h so Printful can download them)
```bash
gh repo create pod-assets --public --add-readme
```

**2.3 Make two tokens** at github.com → click your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**

| Token name | Repository access | Permissions | Save as |
|---|---|---|---|
| `pod-assets-writer` | Only select repositories → `pod-assets` | Repository permissions → **Contents: Read and write** | `ASSETS_GH_TOKEN` |
| `pod-models` | Public repositories (read-only) is fine | Account permissions → **Models: Read** | `GH_MODELS_TOKEN` |

Set expiration to 1 year. Copy each token into your scratch note immediately (GitHub shows it once).

Also note: `ASSETS_GH_REPO` = `YOU/pod-assets` (your username, slash, pod-assets).

---

## PART 3 — Brains (10 min)

**3.1 Claude Pro → Claude Code token**
```bash
npm install -g @anthropic-ai/claude-code
claude                 # first run: pick "Claude account" login, finish in browser, then type /exit
claude setup-token     # prints a long token starting sk-ant-oat...
```
Copy it → scratch note as `CLAUDE_CODE_OAUTH_TOKEN`.

**3.2 Gemini free key (the backup brain)**
Go to **aistudio.google.com** → sign in with Google → left menu **Get API key → Create API key** → pick/create a project → copy.
Save as `GEMINI_API_KEY`. No billing account needed; the free tier is permanent.

---

## PART 4 — Database: Neon (5 min)

1. **neon.tech → Sign up with GitHub** (free, no card).
2. **New project**: name `pod`, region closest to you → **Create**.
3. On the dashboard click **Connect** → choose **Pooled connection**, tick **Show password** → copy the whole `postgresql://...` string. Save as `DATABASE_URL`.
4. Create the tables: in Neon's left menu click **SQL Editor**. On your laptop open `~/pod-autopilot/sql/schema.sql`, copy ALL of it, paste into the editor, click **Run**.
   ✅ Left sidebar **Tables** shows `experiments`, `sales`, `spend`, `events`, `ip_strikes`, `exp_variations`.

---

## PART 5 — Apify: the trademark checker (3 min)

**apify.com → Sign up** (free plan, no card) → top-right avatar → **Settings → API & Integrations → Personal API tokens → copy**. Save as `APIFY_TOKEN`.
(The free plan gives $5 of credit every month; a check costs about half a cent.)

---

## PART 6 — Printful (10 min)

Log in at printful.com.

**6.1 Token:** **Settings (gear) → Stores → your Square store → API** (or **Developers → Tokens**) → **Add token** → name `pod-autopilot`, pick the Square store, all scopes → copy. Save as `PRINTFUL_API_TOKEN`.

**6.2 The four switches** (each one silently breaks automation if wrong):
- [ ] **Settings → Orders → "Manually confirm imported orders" = OFF**
- [ ] **Billing → Billing methods** → there is a card or PayPal. (Printful charges this when a customer order comes in; Square pays you back the next business day. If it bounces, Printful just holds the order until paid.)
- [ ] **Stores** → your Square store shows **Connected** (if not: Stores → Choose platform → Square → authorize)
- [ ] **Stores → Square → Settings → product sync is ON**

---

## PART 7 — Square (10 min)

**7.1 API token:** **developer.squareup.com → Developer Dashboard → + (Create app)** → name `pod-autopilot` → open it → top toggle to **Production** → **Credentials → Production Access Token → Show → copy**. Save as `SQUARE_ACCESS_TOKEN`.
In the same app go to **OAuth** (scopes are only needed for OAuth flows; the personal access token already has full access to your own account — fine for this).

**7.2 Location ID:** still in the app → **Locations** tab → copy the ID of your main location. Save as `SQUARE_LOCATION_ID`.

**7.3 Store URL:** **squareup.com/dashboard → Online → (Overview)** → your site address like `https://yourname.square.site`. Save as `SQUARE_STORE_URL`.

**7.4 The two switches** (replaces the browser-bot from the old plan):
- [ ] **Square Dashboard → Online → Items → Item Sync → Item visibility (default) = Visible**
- [ ] **Square Dashboard → Online → Settings → Square Sync → "Mark newly imported items as unavailable online" = OFF**

---

## PART 8 — Paste everything into GitHub (10 min)

Go to **github.com/YOU/pod-autopilot → Settings → Secrets and variables → Actions**.

**Secrets tab → New repository secret** — add each (name must match exactly):

| Name | From |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Part 3.1 |
| `GEMINI_API_KEY` | Part 3.2 |
| `GH_MODELS_TOKEN` | Part 2.3 |
| `DATABASE_URL` | Part 4 |
| `APIFY_TOKEN` | Part 5 |
| `PRINTFUL_API_TOKEN` | Part 6.1 |
| `SQUARE_ACCESS_TOKEN` | Part 7.1 |
| `SQUARE_LOCATION_ID` | Part 7.2 |
| `ASSETS_GH_TOKEN` | Part 2.3 |

**Variables tab → New repository variable:**

| Name | Value |
|---|---|
| `DRY_RUN` | `1`  ← stays 1 until Part 12 |
| `ASSETS_GH_REPO` | `YOU/pod-assets` |
| `SQUARE_STORE_URL` | `https://yourname.square.site` |
| `STORE_NAME` | your store's name |
| `POD_NICHES` | `nursing,teacher,dogs,plants,fishing,pickleball` (edit to taste) |
| `CAP_NEW_LISTINGS_DAY` | `4` |

Then **Settings → Pages → Build and deployment → Source: GitHub Actions**. (Just set it; nothing to deploy yet.)

---

## PART 9 — First run in the cloud, still fake (10 min)

1. Repo → **Actions** tab → if asked, click **"I understand my workflows, go ahead and enable them"**.
2. Left list → **daily-discover-design → Run workflow → Run workflow** (green button).
3. Click into the run → **run** job. Watch the steps:
   - **readiness** → must end with `0 problem(s)`. Warnings are OK for now.
   - **discover** → prints `discover: N candidate(s)`. (If it says Claude failed and Gemini took over, that's the fallback working, fine.)
   - **design + list** → one line per concept like `scored=0.61 | ip=clear | design=built | mockups=5 | listed @ $27.99 ... | testing=started` with `[DRY_RUN]` on the writes. Some concepts will say `below threshold` or `ip` — that's the gates doing their job.
4. Scroll to the bottom of the run page → **Artifacts** → download, open an `.svg` — that's the art the free lane makes.
5. Now **Actions → ops → Run workflow**. ✅ It goes green and prints a JSON summary.

If **readiness** fails, it tells you exactly which value is missing; fix it in Part 8 and re-run.

---

## PART 10 — Free click counter on Cloudflare (10 min)

1. **dash.cloudflare.com → Sign up** (free, no card). Verify email.
2. On your laptop:
```bash
npm install -g wrangler
cd ~/pod-autopilot/worker/click-counter
wrangler login                          # browser opens, click Allow
wrangler kv namespace create CLICKS     # prints:  id = "abc123..."
```
3. Open `wrangler.toml` in a text editor and replace `REPLACE_WITH_KV_NAMESPACE_ID` with that id. Save.
4. Make a password and deploy:
```bash
openssl rand -hex 24                    # copy the output → save as CLICK_STATS_TOKEN
wrangler secret put STATS_TOKEN         # paste that same value when prompted
wrangler deploy                         # prints: https://pod-go.YOURNAME.workers.dev
```
5. In GitHub: **Secret** `CLICK_STATS_TOKEN` = the hex string; **Variable** `CLICK_WORKER_URL` = the workers.dev URL (no trailing slash).
6. Commit the toml change: `cd ~/pod-autopilot && git add -A && git commit -m "worker id" && git push`

✅ Open the workers.dev URL in a browser → it says "pod-autopilot click counter".

---

## PART 11 — Pinterest: the free traffic (15 min, plus a wait)

**11.1 Turn on the feed page**
- GitHub **Variable** `FEED_SITE_URL` = `https://YOU.github.io/pod-autopilot`
- **Actions → ops → Run workflow** (this writes `public/feed.xml` and pushes it; the **pages** workflow then publishes it).
- ✅ After ~2 min, `https://YOU.github.io/pod-autopilot/feed.xml` opens in a browser (empty feed for now — fine).

**11.2 Pinterest Business account**
- pinterest.com → create an account (or **Settings → Account management → Convert to business**). Free.
- **Settings → Claimed accounts → Websites → Claim** → choose **"Upload HTML file"** → download the file → put it in `~/pod-autopilot/public/` → `git add -A && git commit -m "pinterest" && git push` → wait 2 min → back in Pinterest click **Verify**. Enter the site as `YOU.github.io/pod-autopilot`.
- Create one **board per niche** (e.g. "Nurse T-Shirts", "Teacher Tees").
- Top-right **Create → Bulk create Pins** (sometimes under **Settings → Bulk create Pins**) → **Auto-publish → Connect RSS feed** → paste `https://YOU.github.io/pod-autopilot/feed.xml` → choose a board → **Connect**.

Pinterest checks the feed about once a day. New listings become Pins automatically, each linking through your click counter to the Square product.

---

## PART 12 — Go live (5 min + proof)

1. GitHub **Variables → DRY_RUN → Edit → `0` → Save.** This is the only switch.
2. **Actions → daily-discover-design → Run workflow.**
3. Proof chain — check each:
   - [ ] Run log: a line with `listed @ $xx.xx` and **no** `[DRY_RUN]` on it
   - [ ] **Printful → Products** → the new tee with 5 mockups
   - [ ] **Square Dashboard → Items** → same tee, same price
   - [ ] Your public **square.site** → product visible, Add to Cart works
   - [ ] `github.com/YOU/pod-assets/releases` → a `printfiles` release with the PNG (it will disappear in 48 h — normal)
   - [ ] **Actions → ops → Run workflow** → green; summary shows `"links": 1` or more
4. Close the laptop. Seriously.

---

## WHAT HAPPENS NOW (no action needed)

| When | What runs | What you see |
|---|---|---|
| 07:17 every morning | `daily-discover-design` | up to 4 new listings/day until 12 tests are live, then it waits for kills |
| every 30 min | `ops` | orders & refunds pulled from Square, clicks pulled from the Worker, tests scaled/killed/held, feed rebuilt |
| ~daily | Pinterest | new Pins from your feed |
| Sunday | `weekly-review` | a GitHub **issue** with a plain-English review — this is your 5-minute weekly job |
| only if something is wrong | Sentinel | a GitHub issue labelled **halt**. Fix the cause, then run `python -m pod.cli resume "note"` |

Expect: week 1 zero sales (normal), week 2–3 clicks trickle, week 4–5 first
tests get decided and most are killed. 1–3 sales out of the first 30 designs
is a realistic organic start. The machine learns which niches to drop from
exactly that data.

## IF YOU GET STUCK
- **readiness step fails** → it names the missing value. Part 8.
- **"claude -p exit 1"** in logs → your Claude token lane failed; Gemini takes over automatically. Re-run `claude setup-token` later and update the secret.
- **Printful order sitting in "Needs approval"** → Part 6.2 switch 1 is still on.
- **Product not showing on square.site** → Part 7.4 switches.
- **Anything else** → copy the red log text into Codex/Claude with "step X of START_HERE.md".
