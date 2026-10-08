# POD Autopilot -- operational targets.
# Env comes from your shell / .env; see .env.example for the full list.

PY      ?= python3
PIP     ?= pip

.PHONY: install check validate demo schema report halt resume webhooks mcp agent clean zero-check zero-demo discover design ops review worker

install:                      ## install the pipeline package + dev deps
	$(PIP) install -e ".[dev]"

check:                        ## am I allowed to turn this on? (LIVE=1 for prod)
	$(PY) scripts/setup_check.py

validate:                     ## 88 offline safety checks, no keys/network/db
	DRY_RUN=1 ASSET_BASE_URL=https://assets.example.test $(PY) scripts/validate.py

demo:                         ## full chain offline: score->ip->design->mockup->publish->test
	DRY_RUN=1 POD_MEMORY_DB=1 POD_FAKE_SIGNALS=1 POD_DEMO_NO_USPTO=1 \
	ASSET_BASE_URL=https://assets.example.test \
	$(PY) scripts/run_one.py --niche nursing \
	  --concept "Night Shift Nurse Coffee" \
	  --phrase "STILL RUNNING ON CAFFEINE" \
	  --keywords "night shift nurse,nurse coffee,12 hour shift" \
	  --art-subject "a steaming coffee cup silhouette under a starry night sky"

schema:                       ## create pipeline tables in $DATABASE_URL
	psql "$${DATABASE_URL:-postgresql://pod:pod@localhost:5433/pod}" -f sql/schema.sql

webhooks:                     ## Square/Printful event receiver on :8080 (put behind TLS)
	$(PY) -m pod.webhooks_serve

mcp:                          ## the MCP tool server (normally launched BY OpenBot)
	$(PY) -m pod.mcp_server

agent:                        ## Shape B only: custom AG-UI agent on :9000
	$(PY) agent/pod_agent.py

report:                       ## P&L + funnel + top kill reasons + guardrail state
	$(PY) -m pod.cli report

halt:                         ## THE KILL SWITCH. usage: make halt REASON="..."
	$(PY) -m pod.cli halt "$(REASON)"

resume:                       ## human-only restart. usage: make resume NOTE="..."
	$(PY) -m pod.cli resume "$(NOTE)"

clean:
	rm -rf artifacts/*.png __pycache__ src/pod/__pycache__

# ---------------------------------------------------------------------------
# ZERO-CASH mode (docs/ZERO_CASH_PLAN.md). Same pipeline, free plumbing.
# ---------------------------------------------------------------------------
zero-check:                   ## am I wired up for the $0 deployment?
	TRAFFIC_MODE=organic CAP_WEEKLY_ADS=0 CAP_DAILY_ADS=0 $(PY) scripts/zero_cash_check.py

zero-demo:                    ## offline: vector-art design -> listing, no keys at all
	DRY_RUN=1 POD_MEMORY_DB=1 POD_FAKE_SIGNALS=1 POD_DEMO_NO_USPTO=1 TRAFFIC_MODE=organic \
	ART_PROVIDER=svg ASSET_BASE_URL=https://assets.example.test \
	$(PY) -m pod.batch design --candidates examples/candidates.demo.json

discover:                     ## today's candidates from real public signals + your LLM lane
	$(PY) -m pod.batch discover

design:                       ## push state/candidates.json through score/IP/design/list
	$(PY) -m pod.batch design

ops:                          ## poll orders + clicks, decide tests, run Sentinel, rebuild feed
	$(PY) -m pod.batch ops

review:                       ## weekly plain-English review -> REPORT.md
	$(PY) -m pod.batch review

worker:                       ## deploy the free click counter (needs wrangler login)
	cd worker/click-counter && npx wrangler deploy
