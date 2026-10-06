-- Anomaly: countries with an unusually low daily success rate, and the decline reason behind it
WITH daily AS (
    SELECT country, date_trunc('day', created_at)::date AS day, count(*) AS tx,
           avg((status = 'approved')::int) AS approval,
           mode() WITHIN GROUP (ORDER BY decline_reason) AS top_reason
    FROM transactions GROUP BY 1, 2
),
baseline AS (
    SELECT country, percentile_cont(0.5) WITHIN GROUP (ORDER BY approval) AS median_approval
    FROM daily GROUP BY 1
)
SELECT d.country, d.day, d.tx, round(100 * d.approval, 1) AS approval_pct,
       round((100 * b.median_approval)::numeric, 1) AS baseline_pct,
       round((100 * (d.approval - b.median_approval))::numeric, 1) AS diff_pp, d.top_reason
FROM daily d JOIN baseline b USING (country)
WHERE d.tx >= 100 AND d.approval < b.median_approval - 0.05
ORDER BY diff_pp;
