-- POD Autopilot schema.
-- Run against the OpenBot deployment's Postgres (it already runs pgvector) on a
-- separate database:  createdb pod && psql pod -f schema.sql
--
-- The `data` jsonb column holds the full Experiment snapshot so the state
-- machine can evolve without a migration every time. The typed columns are the
-- ones you query and aggregate on.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS experiments (
    id              TEXT PRIMARY KEY,
    stage           TEXT NOT NULL,
    niche           TEXT,
    concept         TEXT NOT NULL,
    keywords        JSONB DEFAULT '[]'::jsonb,
    audience        TEXT,
    demand_score    NUMERIC(6,4) DEFAULT 0,
    data            JSONB NOT NULL,
    square_item_id  TEXT,
    printful_sync_id BIGINT,
    listed_at       TIMESTAMPTZ,
    orders          INTEGER DEFAULT 0,
    revenue         NUMERIC(12,2) DEFAULT 0,
    refunds         INTEGER DEFAULT 0,
    impressions     INTEGER DEFAULT 0,
    clicks          INTEGER DEFAULT 0,
    add_to_cart     INTEGER DEFAULT 0,
    ad_spend        NUMERIC(12,2) DEFAULT 0,
    kill_reason     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The dedupe index. An autonomous loop rediscovers the same trend daily; this
-- is what stops it rebuilding the same shirt 40 times.
CREATE UNIQUE INDEX IF NOT EXISTS experiments_concept_key
    ON experiments (lower(concept));
CREATE INDEX IF NOT EXISTS experiments_stage_idx ON experiments (stage);
CREATE INDEX IF NOT EXISTS experiments_listed_idx ON experiments (listed_at DESC);
CREATE INDEX IF NOT EXISTS experiments_score_idx ON experiments (demand_score DESC);

-- Square catalog_variation_id -> experiment. This is the attribution link that
-- makes the TESTING stage meaningful. Lose it and you cannot tell which design
-- sold, so you cannot promote or kill on evidence.
CREATE TABLE IF NOT EXISTS exp_variations (
    square_variation_id TEXT PRIMARY KEY,
    experiment_id       TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    sku                 TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS exp_variations_exp_idx ON exp_variations (experiment_id);

-- Sales are append-only and idempotent on (order_id, variation_id). Square
-- retries webhooks; ON CONFLICT DO NOTHING keeps double-counting impossible.
-- Metrics are RECOMPUTED from this table, never incremented in place --
-- incrementing drifts, recomputing converges.
CREATE TABLE IF NOT EXISTS sales (
    id              BIGSERIAL PRIMARY KEY,
    experiment_id   TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    variation_id    TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    qty             INTEGER NOT NULL DEFAULT 1,
    gross           NUMERIC(12,2) NOT NULL DEFAULT 0,
    refunded        BOOLEAN NOT NULL DEFAULT FALSE,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (order_id, variation_id)
);
CREATE INDEX IF NOT EXISTS sales_exp_idx ON sales (experiment_id, at DESC);

-- Every dollar out. The Sentinel reads this to enforce spend caps. Two kinds
-- matter: 'ads' (discretionary, capped daily and weekly) and 'fulfillment'
-- (customer-triggered, capped weekly as a runaway-loop detector).
CREATE TABLE IF NOT EXISTS spend (
    id              BIGSERIAL PRIMARY KEY,
    experiment_id   TEXT REFERENCES experiments(id) ON DELETE SET NULL,
    amount          NUMERIC(12,2) NOT NULL,
    kind            TEXT NOT NULL,           -- 'ads' | 'fulfillment' | 'tools' | 'other'
    ref             TEXT,
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS spend_kind_at_idx ON spend (kind, at DESC);

-- Append-only event log: webhooks, guardrail trips, halts, ops alerts.
-- Separate from OpenBot's own audit table on purpose -- that one records what
-- the Bots DID, this records what happened TO the business.
CREATE TABLE IF NOT EXISTS events (
    id      BIGSERIAL PRIMARY KEY,
    type    TEXT NOT NULL,
    data    JSONB DEFAULT '{}'::jsonb,
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_type_at_idx ON events (type, at DESC);
CREATE INDEX IF NOT EXISTS events_at_idx ON events (at DESC);

-- IP strikes. Threshold is 1. One recorded strike halts the whole machine
-- until a human reviews it.
CREATE TABLE IF NOT EXISTS ip_strikes (
    id              BIGSERIAL PRIMARY KEY,
    experiment_id   TEXT REFERENCES experiments(id) ON DELETE SET NULL,
    source          TEXT,           -- 'uspto' | 'cease_and_desist' | 'platform_takedown' | 'manual'
    term            TEXT,
    detail          JSONB DEFAULT '{}'::jsonb,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Views the reporting and guardrail checks read.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_live_listings AS
SELECT id, concept, niche, demand_score, retail_price, listed_at,
       impressions, clicks, orders, revenue
FROM experiments
WHERE stage IN ('LISTED','TESTING','SCALING')
ORDER BY listed_at DESC;

CREATE OR REPLACE VIEW v_pnl AS
SELECT
  (SELECT COALESCE(SUM(gross),0) FROM sales)                              AS revenue,
  (SELECT COALESCE(SUM(amount),0) FROM spend WHERE kind='fulfillment')    AS cogs,
  (SELECT COALESCE(SUM(amount),0) FROM spend WHERE kind='ads')            AS ads,
  (SELECT COALESCE(SUM(gross),0) FROM sales) * 0.033                      AS square_fees_est,
  (SELECT COALESCE(SUM(amount),0) FROM spend WHERE at > now() - interval '7 days') AS spend_7d;

-- Funnel by stage. This is the first query to run when the machine seems stuck:
-- it shows exactly which gate is rejecting.
CREATE OR REPLACE VIEW v_funnel AS
SELECT stage, count(*) AS n
FROM experiments
GROUP BY stage
ORDER BY n DESC;

-- Which gate is killing designs, and how often. After ~100 experiments this
-- tells you whether your problem is demand (nothing scores), IP (everything
-- gets rejected), or conversion (things list but never sell). Those are three
-- completely different fixes and the funnel view tells you which one you have.
CREATE OR REPLACE VIEW v_kill_reasons AS
SELECT
  split_part(kill_reason, ':', 1) AS gate,
  count(*) AS n,
  round(avg(demand_score)::numeric, 3) AS avg_score
FROM experiments
WHERE stage = 'KILLED' AND kill_reason IS NOT NULL
GROUP BY 1
ORDER BY n DESC;
