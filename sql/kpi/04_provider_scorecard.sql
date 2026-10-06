-- KPI: provider scorecard — approval, latency, estimated fees on approved volume (last 7 days)
SELECT p.name AS provider, count(*) AS tx,
       round(100.0 * avg((t.status = 'approved')::int), 2) AS approval_pct,
       round(avg(t.processing_time_ms)) AS avg_ms,
       round(sum(t.amount_usd) FILTER (WHERE t.status = 'approved')) AS approved_usd,
       round(sum(t.amount_usd * p.fee_pct::numeric / 100) FILTER (WHERE t.status = 'approved')) AS est_fees_usd
FROM transactions t JOIN providers p ON p.provider_id = t.provider
WHERE t.created_at > now() - interval '7 days'
GROUP BY 1 ORDER BY approved_usd DESC NULLS LAST;
