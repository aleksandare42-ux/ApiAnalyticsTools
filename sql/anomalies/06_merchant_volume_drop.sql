-- Anomaly: merchant traffic drop (share of hourly volume vs the merchant's median share)
WITH hourly AS (
    SELECT date_trunc('hour', created_at) AS hour, merchant_id, count(*) AS tx
    FROM transactions GROUP BY 1, 2
),
shares AS (
    SELECT h.*, tx::float / sum(tx) OVER (PARTITION BY hour) AS share,
           sum(tx) OVER (PARTITION BY hour) AS hour_total
    FROM hourly h
),
baseline AS (
    SELECT merchant_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY share) AS median_share
    FROM shares WHERE hour_total >= 200 GROUP BY 1
)
SELECT s.merchant_id, m.name, s.hour, s.tx,
       round((100 * s.share)::numeric, 2) AS share_pct,
       round((100 * b.median_share)::numeric, 2) AS baseline_share_pct,
       round((s.share / b.median_share)::numeric, 2) AS ratio
FROM shares s JOIN baseline b USING (merchant_id) JOIN merchants m USING (merchant_id)
WHERE s.hour_total >= 200 AND s.share < 0.3 * b.median_share
ORDER BY s.merchant_id, s.hour;
