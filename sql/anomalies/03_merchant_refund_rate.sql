-- Anomaly: merchants with an abnormal daily refund rate (vs their own median day)
WITH daily AS (
    SELECT merchant_id, date_trunc('day', created_at)::date AS day,
           count(*) FILTER (WHERE status = 'approved') AS approved,
           count(*) FILTER (WHERE refunded) AS refunds
    FROM transactions GROUP BY 1, 2
),
rates AS (
    SELECT *, refunds::float / nullif(approved, 0) AS refund_rate FROM daily WHERE approved >= 30
),
baseline AS (
    SELECT merchant_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY refund_rate) AS median_rate
    FROM rates GROUP BY 1
)
SELECT r.merchant_id, m.name, m.vertical, r.day, r.approved, r.refunds,
       round((100 * r.refund_rate)::numeric, 2) AS refund_pct,
       round((100 * b.median_rate)::numeric, 2) AS baseline_pct,
       round((r.refund_rate / nullif(b.median_rate, 0))::numeric, 1) AS ratio
FROM rates r JOIN baseline b USING (merchant_id) JOIN merchants m USING (merchant_id)
WHERE r.refund_rate > greatest(3 * b.median_rate, b.median_rate + 0.04)
ORDER BY ratio DESC NULLS LAST;
