"""Generate the historical dataset and load it into Postgres.

    python -m opanalytics.seed --rows 500000 --days 30

A few known anomalies are injected on purpose. They are saved to the
seed_ground_truth table, so we can check what the detector finds.
"""

import argparse
import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import reference as ref
from .db import DATABASE_URL

# start_frac - where in the period the anomaly starts (0..1)
SEED_ANOMALIES = [
    {
        "name": "AlphaPay decline spike",
        "dimension": "provider",
        "dim_value": "alphapay",
        "metric": "approval_rate",
        "start_frac": 0.33,
        "hours": 6,
        "effect": "decline_rate",
        "value": 0.30,
        "reason": "do_not_honor",
        "description": "Provider A suddenly declines +30pp (do_not_honor)",
    },
    {
        "name": "MelodyBox integration outage",
        "dimension": "merchant",
        "dim_value": "m_009",
        "metric": "volume_share",
        "start_frac": 0.50,
        "hours": 12,
        "effect": "volume_mult",
        "value": 0.08,
        "description": "Merchant traffic drops to ~8% of normal",
    },
    {
        "name": "BetaGate latency degradation",
        "dimension": "provider",
        "dim_value": "betagate",
        "metric": "latency",
        "start_frac": 0.60,
        "hours": 36,
        "effect": "latency_ms",
        "value": 900,
        "description": "Provider B processing time +900ms",
    },
    {
        "name": "LuvMatch refund wave",
        "dimension": "merchant",
        "dim_value": "m_017",
        "metric": "refund_rate",
        "start_frac": 0.73,
        "hours": 72,
        "effect": "refund_rate",
        "value": 0.18,
        "description": "Merchant X refund rate +18pp",
    },
    {
        "name": "Brazil success collapse",
        "dimension": "country",
        "dim_value": "BR",
        "metric": "approval_rate",
        "start_frac": 0.83,
        "hours": 20,
        "effect": "decline_rate",
        "value": 0.28,
        "reason": "suspected_fraud",
        "description": "Country Y issuers decline as suspected_fraud (+28pp)",
    },
    {
        "name": "GammaPSP timeouts",
        "dimension": "provider",
        "dim_value": "gammapsp",
        "metric": "error_rate",
        "start_frac": 0.90,
        "hours": 3,
        "effect": "error_rate",
        "value": 0.15,
        "description": "Provider C returns timeouts/5xx for 15% of calls",
    },
]

DIMENSION_COLUMNS = {
    "provider": "provider",
    "merchant": "merchant_id",
    "country": "country",
    "payment_method": "payment_method",
}

COLUMNS = [
    "transaction_id",
    "payment_id",
    "attempt_no",
    "merchant_id",
    "created_at",
    "country",
    "currency",
    "amount",
    "amount_usd",
    "payment_method",
    "provider",
    "status",
    "decline_reason",
    "processing_time_ms",
    "refunded",
    "refunded_at",
    "source",
]

INSERT_TRUTH = """
    INSERT INTO seed_ground_truth
        (name, dimension, dim_value, metric, start_at, end_at, description)
    VALUES (%(name)s, %(dimension)s, %(dim_value)s, %(metric)s,
            %(start_at)s, %(end_at)s, %(description)s)
"""


def anomaly_windows(days, end):
    start = end - timedelta(days=days)
    result = []
    for anomaly in SEED_ANOMALIES:
        begin = start + timedelta(days=days * anomaly["start_frac"])
        begin = begin.replace(minute=0, second=0, microsecond=0)
        finish = begin + timedelta(hours=anomaly["hours"])
        finish = min(finish, end - timedelta(hours=1))
        result.append({**anomaly, "start_at": begin, "end_at": finish})
    return result


def lookup(mapping, keys):
    """Vectorised dict lookup: lookup({'a': 1}, ['a', 'a']) -> array."""
    return np.array([mapping[k] for k in keys])


def make_timestamps(rng, rows, start, end):
    # daily seasonality, a bit more on weekends and slow growth over time
    hours = pd.date_range(
        start.replace(minute=0, second=0), end, freq="h", tz="UTC"
    )[:-1]
    weights = ref.HOURLY_PROFILE[hours.hour.values]
    weights = weights * np.where(hours.dayofweek.values >= 5, 1.12, 1.0)
    weights = weights * np.linspace(0.92, 1.08, len(hours))

    per_hour = rng.multinomial(rows, weights / weights.sum())
    offsets = rng.integers(0, 3_600_000_000, rows).astype("timedelta64[us]")
    ts = np.repeat(hours.values, per_hour) + offsets
    return pd.DatetimeIndex(ts).tz_localize("UTC")


def pick(rng, values, size, p):
    return np.array(values)[rng.choice(len(values), size, p=p)]


def generate(rows=500_000, days=30, end=None, inject=True, seed=42):
    """Return (dataframe, list of injected anomalies)."""
    rng = np.random.default_rng(seed)
    end = (end or datetime.now(timezone.utc)).replace(microsecond=0)
    start = end - timedelta(days=days)
    truth = anomaly_windows(days, end) if inject else []

    ts = make_timestamps(rng, rows, start, end)

    merchant_idx = rng.choice(
        len(ref.MERCHANT_IDS), rows, p=ref.MERCHANT_WEIGHTS
    )
    merchant = np.array(ref.MERCHANT_IDS)[merchant_idx]
    merchants = [ref.MERCHANT_BY_ID[m] for m in ref.MERCHANT_IDS]
    home = np.array([m["home_country"] for m in merchants])[merchant_idx]
    vertical = np.array([m["vertical"] for m in merchants])[merchant_idx]

    from_home = rng.random(rows) < ref.HOME_COUNTRY_SHARE
    other = pick(rng, ref.COUNTRY_CODES, rows, ref.COUNTRY_WEIGHTS)
    country = np.where(from_home, home, other)
    currency = np.array([ref.COUNTRIES[c][0] for c in country])

    method = np.empty(rows, dtype=object)
    for code in ref.COUNTRY_CODES:
        mask = country == code
        method[mask] = pick(
            rng, ref.METHOD_CODES, mask.sum(), ref.method_weights(code)
        )

    median = np.array([ref.VERTICALS[v][0] for v in vertical])
    sigma = np.array([ref.VERTICALS[v][1] for v in vertical])
    amount_usd = np.round(np.exp(rng.normal(np.log(median), sigma)), 2)
    amount_usd = np.maximum(0.99, amount_usd)
    amount = np.round(amount_usd * lookup(ref.FX, currency), 2)

    # routing by weight between providers that support the method
    provider = np.empty(rows, dtype=object)
    for code in ref.METHOD_CODES:
        mask = method == code
        options = [p for p in ref.PROVIDERS if code in p["methods"]]
        weights = np.array([p["weight"] for p in options], dtype=float)
        provider[mask] = pick(
            rng,
            [p["provider_id"] for p in options],
            mask.sum(),
            weights / weights.sum(),
        )

    df = pd.DataFrame(
        {
            "created_at": ts,
            "merchant_id": merchant,
            "country": country,
            "currency": currency,
            "payment_method": method,
            "provider": provider,
            "amount": amount,
            "amount_usd": amount_usd,
        }
    )

    # what the injected anomalies do to each row
    extra_decline = np.zeros(rows)
    extra_latency = np.zeros(rows)
    extra_error = np.zeros(rows)
    extra_refund = np.zeros(rows)
    forced_reason = np.full(rows, None, dtype=object)
    has_forced_reason = np.zeros(rows, dtype=bool)
    keep = np.ones(rows, dtype=bool)

    for anomaly in truth:
        column = DIMENSION_COLUMNS[anomaly["dimension"]]
        mask = (
            (df["created_at"] >= anomaly["start_at"])
            & (df["created_at"] < anomaly["end_at"])
            & (df[column] == anomaly["dim_value"])
        ).values
        effect = anomaly["effect"]
        if effect == "decline_rate":
            extra_decline[mask] += anomaly["value"]
            if anomaly.get("reason"):
                forced_reason[mask] = anomaly["reason"]
                has_forced_reason[mask] = True
        elif effect == "latency_ms":
            extra_latency[mask] += anomaly["value"]
        elif effect == "error_rate":
            extra_error[mask] += anomaly["value"]
        elif effect == "refund_rate":
            extra_refund[mask] += anomaly["value"]
        elif effect == "volume_mult":
            keep[mask] = rng.random(mask.sum()) < anomaly["value"]

    # approve / decline / error
    by_id = ref.PROVIDER_BY_ID
    base_approval = np.array([by_id[p]["base_approval"] for p in provider])
    country_factor = np.array([ref.COUNTRIES[c][2] for c in country])
    method_factor = np.array([ref.METHODS[m][1] for m in method])
    risk = np.array(
        [ref.RISK_FACTOR[ref.MERCHANT_BY_ID[m]["risk_tier"]] for m in merchant]
    )
    p_base = ref.approval_prob(
        base_approval, country_factor, method_factor, risk, amount_usd
    )
    p = np.maximum(0, p_base - extra_decline)

    u = rng.random(rows)
    is_error = rng.random(rows) < ref.BASE_ERROR_RATE + extra_error
    approved = ~is_error & (u < p)
    status = np.where(approved, "approved", "declined")
    status = np.where(is_error, "error", status)

    reason = np.full(rows, None, dtype=object)
    declined = status == "declined"
    reason[declined] = pick(
        rng, ref.DECLINE_CODES, declined.sum(), ref.DECLINE_WEIGHTS
    )
    # part of the declines in an anomaly window are caused by the anomaly
    # itself, those get the anomaly's decline reason
    caused_share = np.divide(
        extra_decline,
        (1 - p_base) + extra_decline,
        out=np.zeros(rows),
        where=extra_decline > 0,
    )
    caused = declined & (rng.random(rows) < caused_share) & has_forced_reason
    reason[caused] = forced_reason[caused]
    reason[is_error] = np.array(ref.ERROR_REASONS)[
        rng.integers(0, 2, is_error.sum())
    ]

    base_latency = np.array([by_id[p]["base_latency_ms"] for p in provider])
    latency_factor = np.array([ref.METHODS[m][2] for m in method])
    mu = ref.latency_mu(base_latency, latency_factor)
    latency = np.exp(rng.normal(mu, ref.LATENCY_SIGMA)) + extra_latency
    # errors are either a timeout or a quick 5xx
    error_latency = np.where(rng.random(rows) < 0.5, 2500, latency * 0.2)
    latency = np.where(is_error, error_latency, latency)

    refund_rate = np.array(
        [ref.MERCHANT_BY_ID[m]["base_refund_rate"] for m in merchant]
    )
    refunded = approved & (rng.random(rows) < refund_rate + extra_refund)
    delay = pd.to_timedelta(rng.exponential(36 * 3600, rows), unit="s")
    refunded_at = df["created_at"] + delay
    refunded_at = refunded_at.where(
        refunded_at < pd.Timestamp(end), pd.Timestamp(end)
    )
    refunded_at = refunded_at.where(refunded)

    random_bytes = rng.integers(0, 256, (rows, 16), dtype=np.uint8)
    ids = [str(uuid.UUID(bytes=bytes(b), version=4)) for b in random_bytes]

    df["transaction_id"] = ids
    df["payment_id"] = ids
    df["attempt_no"] = 1
    df["status"] = status
    df["decline_reason"] = reason
    df["processing_time_ms"] = np.round(latency).astype(int)
    df["refunded"] = refunded
    df["refunded_at"] = refunded_at
    df["source"] = "seed"

    df = df[keep].sort_values("created_at").reset_index(drop=True)
    return df[COLUMNS], truth


def no_progress(pct, message):
    pass


def write(
    df, truth, database_url, replace=True, progress=no_progress, chunk=50_000
):
    import psycopg

    copy_sql = (
        f"COPY transactions ({', '.join(COLUMNS)}) "
        "FROM STDIN WITH (FORMAT csv)"
    )
    total = len(df)

    with psycopg.connect(database_url, autocommit=True) as conn:
        if replace:
            progress(0.02, "removing previous seed data")
            conn.execute("DELETE FROM transactions WHERE source = 'seed'")
            conn.execute("DELETE FROM seed_ground_truth")
            conn.execute("DELETE FROM anomalies WHERE detected_by = 'batch'")

        with conn.cursor() as cur, cur.copy(copy_sql) as copy:
            for i in range(0, total, chunk):
                buf = io.StringIO()
                df.iloc[i : i + chunk].to_csv(
                    buf,
                    header=False,
                    index=False,
                    date_format="%Y-%m-%d %H:%M:%S.%f%z",
                )
                copy.write(buf.getvalue())
                done = min(i + chunk, total)
                progress(
                    0.05 + 0.85 * done / total, f"COPY {done:,}/{total:,} rows"
                )

        for anomaly in truth:
            conn.execute(INSERT_TRUTH, anomaly)
        progress(0.95, "ANALYZE")
        conn.execute("ANALYZE transactions")
    progress(1.0, f"done: {total:,} rows")


def print_progress(pct, message):
    print(f"[{pct * 100:5.1f}%] {message}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=500_000)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--no-anomalies", action="store_true")
    parser.add_argument(
        "--append", action="store_true", help="keep previous seed rows"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--csv", help="also save the dataset to a CSV file")
    args = parser.parse_args()

    df, truth = generate(
        args.rows, args.days, inject=not args.no_anomalies, seed=args.seed
    )
    if args.csv:
        df.to_csv(args.csv, index=False)
    url = os.environ.get("DATABASE_URL", DATABASE_URL)
    write(df, truth, url, replace=not args.append, progress=print_progress)


if __name__ == "__main__":
    main()
