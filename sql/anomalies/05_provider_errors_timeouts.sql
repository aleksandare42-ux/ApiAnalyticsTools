-- Anomaly: provider technical errors / timeouts (hours with error rate above 5%)
SELECT provider, date_trunc('hour', created_at) AS hour, count(*) AS tx,
       count(*) FILTER (WHERE decline_reason = 'provider_timeout')     AS timeouts,
       count(*) FILTER (WHERE decline_reason = 'provider_unavailable') AS unavailable,
       round(100.0 * avg((status = 'error')::int), 2) AS error_pct
FROM transactions
GROUP BY 1, 2
HAVING count(*) >= 30 AND avg((status = 'error')::int) > 0.05
ORDER BY hour;
