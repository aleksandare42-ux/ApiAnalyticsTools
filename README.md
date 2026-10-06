# Payment Operations Analytics Simulator

A local sandbox for a **payment operations analyst**. It runs a small payment platform: a merchant traffic generator, a payment gateway with routing and cascading, four simulated PSPs, PostgreSQL, an anomaly detector and a control-center dashboard. You can inject incidents into it, watch them show up in the metrics and catch them with SQL, Python or BI.

```
 traffic :8002 ──POST /v1/payments──▶ gateway :8000 ──/{provider}/authorize──▶ providers :8001
 (merchant simulator,                 (weighted routing,                      AlphaPay · BetaGate ·
  refunds, volume)                     cascade, smart routing)                 GammaPSP · DeltaAcq
        │                                   │ INSERT every attempt                  ▲
        └────── settings / chaos rules ──▶ PostgreSQL :5432 ◀──── chaos_rules ───────┘
                                            ▲          ▲
                    detector :8003 ─────────┘          └──── dashboard :8080 (UI + API)
                    (live z-tests + history scan)            Tableau / Metabase / Adminer
```

## Quick start

```bash
python opa.py up          # starts everything and opens http://localhost:8080
python opa.py seed        # 500k historical transactions over 30 days, 6 injected anomalies (~35 s)
python opa.py traffic start 25
python opa.py demo        # incident: AlphaPay +30pp declines -> detection -> smart routing -> recovery
```

`opa.py` uses only the standard library and picks the run mode automatically:

| Mode | When | What runs |
|---|---|---|
| **Docker** | Docker Desktop is running | `docker compose` with Postgres 16, 5 app containers, Adminer, optional Metabase (`--bi`), and hot reload of `./app` |
| **Local** | no Docker (or `--local`) | portable PostgreSQL 16 (downloaded once, no admin rights) plus 5 Python processes in `.venv` |

In local mode the database lives in `%LOCALAPPDATA%\opanalytics` (or `~/.local/share/opanalytics`), not next to the code, because writing to a USB stick is about 50x slower. You can override the location with `OPA_DATA_DIR`.

## What you can do

| Where | What |
|---|---|
| **Overview** | Approval / conversion (after cascade) / decline / error / refund rate, volume, avg & p95 latency, deltas vs the previous window, per-provider time series, decline-reason mix, open anomalies |
| **Breakdown** | Any KPI by provider / country / merchant / method / currency / decline reason; country × provider approval heatmap |
| **Anomalies** | Live and historical incidents with severity, z-score and root-cause grouping. *Investigate* opens SQL Lab with a ready-made query. Recall/precision is scored against the injected ground truth |
| **Live feed** | Every attempt as it happens, with cascaded retries marked `#2` |
| **Traffic & routing** | Start/stop, TPS, bursts; cascading, smart routing, timeout; per-provider enable/weight/base approval/latency; detector window and sensitivity |
| **Chaos lab** | 7 incident presets plus custom rules: `decline_rate`, `latency_ms`, `error_rate`, `refund_rate`, `volume_mult` on any provider/country/merchant/method, with auto-expiry |
| **Payment tester** | Send one or N payments through the real gateway (pick merchant, country, method, or force a provider), then refund |
| **Dataset & export** | Generate a dataset of any size, CSV export for Tableau/Excel, reset |
| **SQL lab** | Read-only SQL over the whole DB, with the queries from `sql/` as examples |
| **Services** | Health of every service, links to each Swagger `/docs`, DB info |

Every service has Swagger: gateway http://localhost:8000/docs, PSPs :8001/docs, traffic :8002/docs, detector :8003/docs, dashboard API :8080/docs.

## CLI

```
python opa.py status | logs [svc] [-f] | restart [svc] | down [-v]
python opa.py seed --rows 1000000 --days 60
python opa.py traffic start 40 | stop | burst 2000 | stats
python opa.py chaos presets | inject provider_latency --minutes 10 | list | clear
python opa.py demo --preset country_low_success      # any preset
python opa.py detect [--history]   |  python opa.py anomalies --status all
python opa.py pay --merchant m_017 --country MX --amount 30 -n 20
python opa.py sql "SELECT provider, count(*) FROM transactions GROUP BY 1"
python opa.py test                 # unit tests (no DB needed)
python opa.py psql                 # SQL shell
```

## Data model

`transactions` has one row per **attempt**. A payment that was cascaded to a second provider has two rows with the same `payment_id`.

`transaction_id, payment_id, attempt_no, merchant_id, created_at, country, currency, amount, amount_usd, payment_method, provider, status (approved|declined|error), decline_reason, processing_time_ms, refunded, refunded_at, source (seed|live)`

Other tables: `merchants`, `providers`, `chaos_rules`, `settings`, `anomalies`, `seed_ground_truth`.
BI views: `v_transactions`, `v_hourly_provider`, `v_daily_kpis`, `v_daily_merchant`, `v_country_provider`.

## Injected anomalies (historical seed)

| # | Anomaly | Slice | Metric |
|---|---|---|---|
| 1 | Provider A suddenly declines +30pp for 6 h | provider=alphapay | approval_rate |
| 2 | Merchant integration outage, traffic at 8% for 12 h | merchant=m_009 | volume_share |
| 3 | Provider B latency +900 ms for 36 h | provider=betagate | latency |
| 4 | Merchant X refund wave +18pp for 3 days | merchant=m_017 | refund_rate |
| 5 | Country Y issuers decline as suspected_fraud for 20 h | country=BR | approval_rate |
| 6 | Provider C timeouts / 5xx at 15% for 3 h | provider=gammapsp | error_rate |

They can be found with the SQL in [`sql/anomalies/`](sql/anomalies), with the Python detector (*Scan history*) and in Tableau. With the default seed the detector reaches **recall 100%** and **precision ≈ 86%**.

## How detection works

* **Live** (every 20 s): the last 5 min of live traffic is compared with a 60-min baseline that ends 5 min earlier. The gap stops an ongoing incident from leaking into its own baseline. Checks used:
  * two-proportion z-test for approval and error rate;
  * Welch z plus a ratio for latency;
  * rate ratio for refunds;
  * Poisson z on share of traffic for volume, which stays robust when TPS changes.

  Incidents are opened, updated and auto-resolved.
* **History**: hourly buckets per slice are compared against that slice's robust baseline (median/MAD). Consecutive flagged hours are merged into one incident.
* **Root-cause grouping**: when a slice is explained by a broader concurrent incident, it is marked *"likely explained by provider=betagate"* instead of being reported as a separate incident. For example, during a BetaGate latency spike every merchant's latency rises too.

## BI tools: Tableau, Power BI, Excel

Open **BI tools** in the dashboard (http://localhost:8080/#bi). It has:

* the connection details for the read-only user `analyst` / `analyst`, plus a button that checks access;
* **Tableau**: a `.tds` data source per dataset (live PostgreSQL in Tableau Desktop) and a `payops.hyper`
  extract with all datasets, which opens in the free Tableau Public;
* **Power BI**: `.pbids` connection files (PostgreSQL Import / DirectQuery, or a CSV over HTTP),
  ready-made Power Query (M) code and DAX measures;
* **OData v4 feed** at http://localhost:8080/odata, which works in Power BI, Excel and Tableau without
  any database driver;
* CSV links for every dataset.

The `.hyper` extract needs the optional Tableau Hyper API: `.venv\Scripts\pip install -r requirements-bi.txt`.

Learning course in Russian, with exercises to find the injected anomalies: [bi/GUIDE_RU.md](bi/GUIDE_RU.md).
Calculated fields for Tableau: [tableau/README.md](tableau/README.md).

## Project layout

```
opa.py                     CLI: run / control / test
docker-compose.yml, Dockerfile
app/opanalytics/
  reference.py             merchants, providers, countries, approval & latency model
  chaos.py                 incident rules + presets
  routing.py               weighted routing, smart routing (rolling health)
  detection.py             statistics, live & batch detectors, root-cause grouping, scoring
  seed.py                  vectorised 500k generator + COPY
  services/                gateway, providers, traffic, detector, dashboard (FastAPI)
  static/                  dashboard UI (vanilla JS + Chart.js)
sql/anomalies, sql/kpi     analyst queries
tests/                     pytest
```
