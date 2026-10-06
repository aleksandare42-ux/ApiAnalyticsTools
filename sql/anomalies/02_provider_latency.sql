-- Anomaly: provider processing-time degradation (hourly avg & p95 vs median hour)
WITH hourly AS (
    SELECT provider, date_trunc('hour', created_at) AS hour, count(*) AS tx,
           avg(processing_time_ms) FILTER (WHERE status <> 'error') AS avg_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY processing_time_ms) AS p95_ms
    FROM transactions GROUP BY 1, 2
),
baseline AS (
    SELECT provider, percentile_cont(0.5) WITHIN GROUP (ORDER BY avg_ms) AS median_avg_ms
    FROM hourly WHERE tx >= 30 GROUP BY 1
)
SELECT h.provider, h.hour, h.tx, round(h.avg_ms) AS avg_ms, round(h.p95_ms::numeric) AS p95_ms,
       round(b.median_avg_ms::numeric) AS baseline_avg_ms,
       round((h.avg_ms / b.median_avg_ms)::numeric, 2) AS ratio
FROM hourly h JOIN baseline b USING (provider)
WHERE h.tx >= 30 AND h.avg_ms > 1.5 * b.median_avg_ms
ORDER BY h.provider, h.hour;
