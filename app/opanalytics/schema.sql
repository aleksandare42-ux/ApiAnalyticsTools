-- Idempotent schema; applied by every service on startup (under an advisory lock).

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id      text PRIMARY KEY,
    name             text NOT NULL,
    vertical         text NOT NULL,
    home_country     char(2) NOT NULL,
    risk_tier        text NOT NULL,
    volume_weight    double precision NOT NULL,
    base_refund_rate double precision NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
    provider_id     text PRIMARY KEY,
    name            text NOT NULL,
    enabled         boolean NOT NULL DEFAULT true,
    weight          integer NOT NULL DEFAULT 25,
    base_approval   double precision NOT NULL,
    base_latency_ms integer NOT NULL,
    methods         text[] NOT NULL,
    fee_pct         double precision NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id     uuid PRIMARY KEY,
    payment_id         uuid NOT NULL,
    attempt_no         smallint NOT NULL DEFAULT 1,
    merchant_id        text NOT NULL,
    created_at         timestamptz NOT NULL,
    country            char(2) NOT NULL,
    currency           char(3) NOT NULL,
    amount             numeric(14,2) NOT NULL,
    amount_usd         numeric(14,2) NOT NULL,
    payment_method     text NOT NULL,
    provider           text NOT NULL,
    status             text NOT NULL CHECK (status IN ('approved','declined','error')),
    decline_reason     text,
    processing_time_ms integer NOT NULL,
    refunded           boolean NOT NULL DEFAULT false,
    refunded_at        timestamptz,
    source             text NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS tx_created_idx  ON transactions (created_at);
CREATE INDEX IF NOT EXISTS tx_provider_idx ON transactions (provider, created_at);
CREATE INDEX IF NOT EXISTS tx_merchant_idx ON transactions (merchant_id, created_at);
CREATE INDEX IF NOT EXISTS tx_country_idx  ON transactions (country, created_at);
CREATE INDEX IF NOT EXISTS tx_payment_idx  ON transactions (payment_id);
CREATE INDEX IF NOT EXISTS tx_refunded_idx ON transactions (refunded_at) WHERE refunded;

CREATE TABLE IF NOT EXISTS chaos_rules (
    id           serial PRIMARY KEY,
    name         text NOT NULL,
    target_type  text NOT NULL,
    target_value text,
    effect       text NOT NULL,
    value        double precision NOT NULL,
    reason       text,
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz
);

CREATE TABLE IF NOT EXISTS settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id   bigserial PRIMARY KEY,
    detected_by  text NOT NULL,              -- live | batch
    dimension    text NOT NULL,              -- provider | country | merchant | payment_method
    dim_value    text NOT NULL,
    metric       text NOT NULL,              -- approval_rate | latency | error_rate | refund_rate | volume_share
    status       text NOT NULL DEFAULT 'open',
    severity     text NOT NULL,
    observed     double precision,
    expected     double precision,
    z_score      double precision,
    sample_size  integer,
    window_start timestamptz,
    window_end   timestamptz,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    message      text
);
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS correlated_with text;
CREATE UNIQUE INDEX IF NOT EXISTS anomalies_open_key
    ON anomalies (detected_by, dimension, dim_value, metric) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS seed_ground_truth (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    dimension   text NOT NULL,
    dim_value   text NOT NULL,
    metric      text NOT NULL,
    start_at    timestamptz NOT NULL,
    end_at      timestamptz NOT NULL,
    description text
);

-- ---------------------------------------------------------------- BI views (Tableau / Metabase)
DROP VIEW IF EXISTS v_transactions, v_hourly_provider, v_daily_kpis, v_daily_merchant, v_country_provider;

CREATE VIEW v_transactions AS
SELECT t.*, m.name AS merchant_name, m.vertical, m.risk_tier, p.name AS provider_name,
       (t.status = 'approved')::int AS is_approved,
       (t.status = 'declined')::int AS is_declined,
       (t.status = 'error')::int    AS is_error,
       t.refunded::int              AS is_refunded
FROM transactions t
LEFT JOIN merchants m USING (merchant_id)
LEFT JOIN providers p ON p.provider_id = t.provider;

CREATE VIEW v_hourly_provider AS
SELECT date_trunc('hour', created_at) AS hour, provider,
       count(*) AS tx,
       count(*) FILTER (WHERE status = 'approved') AS approved,
       count(*) FILTER (WHERE status = 'declined') AS declined,
       count(*) FILTER (WHERE status = 'error')    AS errors,
       round(avg(processing_time_ms))              AS avg_processing_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY processing_time_ms) AS p95_processing_ms,
       sum(amount_usd) FILTER (WHERE status = 'approved') AS approved_volume_usd
FROM transactions GROUP BY 1, 2;

CREATE VIEW v_daily_kpis AS
SELECT date_trunc('day', created_at)::date AS day,
       count(*) AS tx,
       count(DISTINCT payment_id) AS payments,
       round(100.0 * count(*) FILTER (WHERE status = 'approved') / count(*), 2) AS approval_rate_pct,
       round(100.0 * count(*) FILTER (WHERE status = 'declined') / count(*), 2) AS decline_rate_pct,
       round(100.0 * count(*) FILTER (WHERE status = 'error') / count(*), 2)    AS error_rate_pct,
       round(avg(processing_time_ms)) AS avg_processing_ms,
       sum(amount_usd) FILTER (WHERE status = 'approved') AS approved_volume_usd,
       round(100.0 * count(*) FILTER (WHERE refunded) / NULLIF(count(*) FILTER (WHERE status = 'approved'), 0), 2) AS refund_rate_pct
FROM transactions GROUP BY 1;

CREATE VIEW v_daily_merchant AS
SELECT date_trunc('day', t.created_at)::date AS day, t.merchant_id, m.name AS merchant_name, m.vertical,
       count(*) AS tx,
       count(*) FILTER (WHERE status = 'approved') AS approved,
       count(*) FILTER (WHERE refunded) AS refunds,
       round(100.0 * count(*) FILTER (WHERE refunded) / NULLIF(count(*) FILTER (WHERE status = 'approved'), 0), 2) AS refund_rate_pct,
       sum(amount_usd) FILTER (WHERE status = 'approved') AS approved_volume_usd
FROM transactions t LEFT JOIN merchants m USING (merchant_id) GROUP BY 1, 2, 3, 4;

CREATE VIEW v_country_provider AS
SELECT country, provider, count(*) AS tx,
       round(100.0 * count(*) FILTER (WHERE status = 'approved') / count(*), 2) AS approval_rate_pct,
       round(avg(processing_time_ms)) AS avg_processing_ms
FROM transactions GROUP BY 1, 2;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst') THEN
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst;
    END IF;
END $$;
