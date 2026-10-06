-- KPI: cascading effect — attempt-level approval vs payment-level conversion (live traffic)
SELECT date_trunc('minute', created_at) AS minute,
       count(*) AS attempts,
       count(DISTINCT payment_id) AS payments,
       round(100.0 * avg((status = 'approved')::int), 2) AS attempt_approval_pct,
       round(100.0 * count(DISTINCT payment_id) FILTER (WHERE status = 'approved')
             / count(DISTINCT payment_id), 2) AS payment_conversion_pct,
       count(*) FILTER (WHERE attempt_no = 2) AS cascaded_attempts,
       count(*) FILTER (WHERE attempt_no = 2 AND status = 'approved') AS rescued_by_cascade
FROM transactions WHERE source = 'live'
GROUP BY 1 ORDER BY 1 DESC LIMIT 60;
