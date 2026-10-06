-- Live check: last 5 minutes vs the previous hour per provider (what the live detector does)
WITH w AS (
    SELECT provider, created_at >= now() - interval '5 minutes' AS cur, status, processing_time_ms
    FROM transactions
    WHERE source = 'live' AND created_at >= now() - interval '65 minutes'
)
SELECT provider,
       count(*) FILTER (WHERE cur) AS cur_tx,
       round(100.0 * avg((status = 'approved')::int) FILTER (WHERE cur), 1)     AS cur_approval_pct,
       round(100.0 * avg((status = 'approved')::int) FILTER (WHERE NOT cur), 1) AS base_approval_pct,
       round(avg(processing_time_ms) FILTER (WHERE cur))     AS cur_avg_ms,
       round(avg(processing_time_ms) FILTER (WHERE NOT cur)) AS base_avg_ms
FROM w GROUP BY provider ORDER BY provider;
