-- Anomaly: provider approval-rate drops (hourly vs the provider's median hour)
WITH hourly AS (
    SELECT provider, date_trunc('hour', created_at) AS hour, count(*) AS tx,
           avg((status = 'approved')::int) AS approval
    FROM transactions
    GROUP BY 1, 2
),
baseline AS (
    SELECT provider, percentile_cont(0.5) WITHIN GROUP (ORDER BY approval) AS median_approval
    FROM hourly WHERE tx >= 30 GROUP BY 1
)
SELECT h.provider, h.hour, h.tx,
       round(100 * h.approval, 1)                        AS approval_pct,
       round((100 * b.median_approval)::numeric, 1)      AS baseline_pct,
       round((100 * (h.approval - b.median_approval))::numeric, 1) AS diff_pp,
       round(((h.approval - b.median_approval)
             / sqrt(b.median_approval * (1 - b.median_approval) / h.tx))::numeric, 1) AS z_score
FROM hourly h JOIN baseline b USING (provider)
WHERE h.tx >= 30
  AND h.approval < b.median_approval - 0.08
ORDER BY z_score
LIMIT 50;
