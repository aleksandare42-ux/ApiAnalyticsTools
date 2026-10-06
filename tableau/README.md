# Tableau: Payment Operations Dashboard

The connection files (`.tds`, `.hyper`) are on the **BI tools** page of the dashboard,
and the step by step course is in [../bi/GUIDE_RU.md](../bi/GUIDE_RU.md).

## Connect

**Live connection (recommended):** Tableau Desktop → *Connect → PostgreSQL*

| Field | Value |
|---|---|
| Server | `localhost` |
| Port | `5432` |
| Database | `opanalytics` |
| Username / Password | `analyst` / `analyst` (read-only) |

**Tableau Public (no PostgreSQL connector):** in the dashboard open *Dataset & export* and download the CSVs (`hourly_provider.csv`, `daily_kpis.csv`, `daily_merchant.csv`, `transactions.csv`, `anomalies.csv`), or call `GET http://localhost:8080/api/export/<name>.csv?window=30d`.

## Data sources

| View | Grain | Use for |
|---|---|---|
| `v_transactions` | attempt | everything; includes `is_approved`, `is_declined`, `is_error`, `is_refunded` flags plus merchant/provider names |
| `v_hourly_provider` | hour × provider | Provider Performance, latency trend |
| `v_daily_kpis` | day | KPI header |
| `v_daily_merchant` | day × merchant | refund-rate anomalies |
| `v_country_provider` | country × provider | heatmap |
| `anomalies` | incident | anomaly markers / table |

## Calculated fields (on `v_transactions`)

```
Payment Success Rate   SUM([Is Approved]) / COUNT([Transaction Id])
Decline Rate           SUM([Is Declined]) / COUNT([Transaction Id])
Error Rate             SUM([Is Error])    / COUNT([Transaction Id])
Refund Rate            SUM([Is Refunded]) / SUM([Is Approved])
Conversion             COUNTD(IF [Is Approved] = 1 THEN [Payment Id] END) / COUNTD([Payment Id])
Approved Volume USD    SUM(IF [Is Approved] = 1 THEN [Amount Usd] END)
Avg Processing Time    AVG([Processing Time Ms])
Success Rate Δ vs 7d   [Payment Success Rate] - WINDOW_AVG([Payment Success Rate], -168, -1)   // hourly table calc
```

## Suggested sheets

1. **KPI header**: Success rate, Decline rate, Volume, Avg processing time, Refund rate (BANs with % change vs the previous period).
2. **Payment Success Rate over time by provider**: line chart, hourly. The AlphaPay drop and the BR day stand out.
3. **Provider Performance**: table with approval, error rate, avg/p95 latency and volume per provider.
4. **Average Processing Time**: hourly line per provider. Shows the BetaGate latency plateau.
5. **Transaction Volume**: stacked bars by status. Filter by merchant to see the MelodyBox (m_009) gap.
6. **Refund rate by merchant**: daily heat table from `v_daily_merchant`. LuvMatch (m_017) lights up.
7. **Anomalies**: the `anomalies` table joined on `dim_value`, shown as reference bands (`window_start` → `window_end`) on charts 2 and 4.
