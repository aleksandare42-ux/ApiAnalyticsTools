-- KPI: decline / error reason mix per provider (last 24h)
SELECT provider, decline_reason, count(*) AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY provider), 1) AS pct_of_provider_declines
FROM transactions
WHERE status <> 'approved' AND created_at > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 1, n DESC;
