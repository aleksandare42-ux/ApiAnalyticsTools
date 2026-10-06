"""Anomaly detection. No database access here, only math on rows/frames.

Live checks compare the last few minutes with a baseline period:
  * approval / error rate - two-proportion z-test
  * latency - Welch z-test plus a minimal ratio
  * refunds - refund rate vs baseline
  * volume - share of traffic vs baseline share (so it doesn't fire when
    the overall TPS changes)

The batch scan works on hourly buckets of the whole history. Every value
(a provider, a country...) is compared to its own median hour, flagged
hours that are close to each other are merged into one incident.
"""

import math
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd

# dimension name -> column in the transactions table
DIMENSIONS = {
    "provider": "provider",
    "country": "country",
    "merchant": "merchant_id",
    "payment_method": "payment_method",
}

METRIC_LABELS = {
    "approval_rate": "Approval rate",
    "error_rate": "Error rate",
    "latency": "Avg processing time",
    "refund_rate": "Refund rate",
    "volume_share": "Traffic share",
}

# For these metrics the provider is the natural root cause: when a
# provider gets slow, every merchant and country gets slow as well.
ROOT_DIMENSIONS = {
    "latency": ("provider",),
    "error_rate": ("provider",),
}


@dataclass
class Finding:
    dimension: str
    dim_value: str
    metric: str
    observed: float
    expected: float
    z_score: float
    sample_size: int
    severity: str = "medium"
    message: str = ""
    window_start: datetime = None
    window_end: datetime = None
    correlated_with: str = None

    def __post_init__(self):
        self.severity = severity_for(self.metric, self.observed, self.expected)
        if not self.message:
            self.message = describe(self)

    def as_dict(self):
        return asdict(self)


@dataclass
class Thresholds:
    min_n: int = 50
    min_base_n: int = 100
    z: float = 4.0
    min_rate_drop: float = 0.05
    min_error_rise: float = 0.03
    latency_ratio: float = 1.4
    latency_min_ms: float = 120
    refund_ratio: float = 3.0
    refund_min_rise: float = 0.04
    refund_min_count: int = 8
    volume_drop_ratio: float = 0.4
    volume_spike_ratio: float = 2.5
    volume_min_expected: float = 30


DEFAULT_THRESHOLDS = Thresholds()


def two_prop_z(x1, n1, x2, n2):
    """z-score of p1 - p2 (pooled variance). Positive when p1 > p2."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(max(p * (1 - p) * (1 / n1 + 1 / n2), 1e-12))
    return (x1 / n1 - x2 / n2) / se


def welch_z(mean1, sd1, n1, mean2, sd2, n2):
    if not n1 or not n2 or mean1 is None or mean2 is None:
        return 0.0
    var = (sd1 or 0) ** 2 / n1 + (sd2 or 0) ** 2 / n2
    return (mean1 - mean2) / math.sqrt(max(var, 1e-9))


def poisson_z(observed, expected):
    return (observed - expected) / math.sqrt(max(expected, 1e-9))


def severity_for(metric, observed, expected):
    if metric in ("approval_rate", "error_rate"):
        diff = abs(observed - expected)
        if diff >= 0.2:
            return "critical"
        if diff >= 0.08:
            return "high"
        return "medium"

    ratio = observed / expected if expected else float("inf")
    if metric == "volume_share" and ratio < 1:
        ratio = 1 / max(ratio, 1e-9)

    limits = {
        "latency": (3, 2),
        "refund_rate": (6, 3.5),
        "volume_share": (5, 2.5),
    }
    if metric not in limits:
        return "medium"
    critical, high = limits[metric]
    if ratio >= critical:
        return "critical"
    if ratio >= high:
        return "high"
    return "medium"


def format_value(metric, value):
    if metric == "latency":
        return f"{value:,.0f} ms"
    return f"{value * 100:.1f}%"


def describe(f):
    label = METRIC_LABELS.get(f.metric, f.metric)
    if f.metric in ("approval_rate", "error_rate"):
        change = f"{(f.observed - f.expected) * 100:+.1f}pp"
    elif f.expected:
        change = f"x{f.observed / f.expected:.1f}"
    else:
        change = "new"

    observed = format_value(f.metric, f.observed)
    expected = format_value(f.metric, f.expected)
    return (
        f"{label} for {f.dimension}={f.dim_value}: {observed} vs {expected}"
        f" expected ({change}, z={f.z_score:.1f}, n={f.sample_size})"
    )


# Live checks


def check_window_row(dimension, row, th=DEFAULT_THRESHOLDS):
    """Check one dimension value: current window vs baseline.

    The row comes from the detector's SQL: v, cur_n, cur_ok, cur_err,
    cur_lat, cur_lat_sd, base_n, base_ok, base_err, base_lat, base_lat_sd.
    """
    findings = []
    cur_n = row["cur_n"] or 0
    base_n = row["base_n"] or 0
    if cur_n < th.min_n or base_n < th.min_base_n:
        return findings
    value = str(row["v"])

    cur_rate = row["cur_ok"] / cur_n
    base_rate = row["base_ok"] / base_n
    z = two_prop_z(row["cur_ok"], cur_n, row["base_ok"], base_n)
    if base_rate - cur_rate >= th.min_rate_drop and z <= -th.z:
        findings.append(
            Finding(
                dimension,
                value,
                "approval_rate",
                cur_rate,
                base_rate,
                z,
                cur_n,
            )
        )

    cur_err = row["cur_err"] / cur_n
    base_err = row["base_err"] / base_n
    z = two_prop_z(row["cur_err"], cur_n, row["base_err"], base_n)
    if cur_err - base_err >= th.min_error_rise and z >= th.z:
        findings.append(
            Finding(
                dimension, value, "error_rate", cur_err, base_err, z, cur_n
            )
        )

    if row.get("cur_lat") is not None and row.get("base_lat"):
        cur_lat = float(row["cur_lat"])
        base_lat = float(row["base_lat"])
        z = welch_z(
            cur_lat,
            float(row.get("cur_lat_sd") or 0),
            cur_n,
            base_lat,
            float(row.get("base_lat_sd") or 0),
            base_n,
        )
        slower = cur_lat >= th.latency_ratio * base_lat
        if slower and cur_lat - base_lat >= th.latency_min_ms and z >= th.z:
            findings.append(
                Finding(
                    dimension, value, "latency", cur_lat, base_lat, z, cur_n
                )
            )
    return findings


def check_refund_row(row, th=DEFAULT_THRESHOLDS):
    """Refund check for one merchant: v, cur_ok, cur_ref, base_ok, base_ref."""
    cur_ok = row["cur_ok"] or 0
    cur_ref = row["cur_ref"] or 0
    if cur_ok < th.min_n or cur_ref < th.refund_min_count:
        return []

    base_ok = max(row["base_ok"] or 0, 1)
    base_ref = row["base_ref"] or 0
    cur_rate = cur_ref / cur_ok
    base_rate = base_ref / base_ok
    z = two_prop_z(cur_ref, cur_ok, base_ref, base_ok)

    limit = max(th.refund_ratio * base_rate, base_rate + th.refund_min_rise)
    if cur_rate >= limit and z >= th.z:
        return [
            Finding(
                "merchant",
                str(row["v"]),
                "refund_rate",
                cur_rate,
                base_rate,
                z,
                int(cur_ok),
            )
        ]
    return []


def check_volume_rows(dimension, rows, th=DEFAULT_THRESHOLDS):
    """Compare each value's share of traffic with its baseline share."""
    cur_total = sum(r["cur_n"] or 0 for r in rows)
    base_total = sum(r["base_n"] or 0 for r in rows)
    if cur_total < 300 or base_total < 1000:
        return []

    findings = []
    for row in rows:
        base_share = (row["base_n"] or 0) / base_total
        if base_share == 0:
            continue
        expected = base_share * cur_total
        if expected < th.volume_min_expected:
            continue

        observed = row["cur_n"] or 0
        ratio = observed / expected
        z = poisson_z(observed, expected)
        dropped = ratio <= th.volume_drop_ratio and z <= -th.z
        spiked = ratio >= th.volume_spike_ratio and z >= th.z
        if dropped or spiked:
            findings.append(
                Finding(
                    dimension,
                    str(row["v"]),
                    "volume_share",
                    observed / cur_total,
                    base_share,
                    z,
                    int(observed),
                )
            )
    return findings


# Batch scan over the history


def merge_runs(flags, gap):
    """Split flagged rows into groups, a new group starts after `gap`."""
    if flags.empty:
        return []
    flags = flags.sort_values("t")
    gaps = np.diff(flags["t"].to_numpy()) > gap.to_timedelta64()
    group = np.concatenate([[0], np.cumsum(gaps)])
    return [flags.iloc[np.flatnonzero(group == g)] for g in np.unique(group)]


def _incident(dimension, value, metric, run, observed, expected, z, n, length):
    return Finding(
        dimension,
        str(value),
        metric,
        float(observed),
        expected,
        float(z),
        int(n),
        window_start=run["t"].min(),
        window_end=run["t"].max() + length,
    )


def detect_hourly(df, dimension, z_thr=5.0, min_n=30):
    """Find incidents in hourly data.

    df columns: t (hour), v (dimension value), n, ok, err, lat.
    """
    findings = []
    if df.empty:
        return findings

    hour = pd.Timedelta(hours=1)
    gap = pd.Timedelta(hours=2)

    # hour x value table for the traffic share check, quiet hours skipped
    counts = df.pivot_table(
        index="t", columns="v", values="n", aggfunc="sum", fill_value=0
    )
    counts = counts[counts.sum(axis=1) >= 200]
    totals = counts.sum(axis=1).to_numpy(dtype=float)

    for value, g in df.groupby("v"):
        g = g.sort_values("t")
        big = g[g["n"] >= min_n]
        if len(big) < 12:
            continue

        # approval rate drops
        rate = big["ok"] / big["n"]
        p0 = float(rate.median())
        z = (rate - p0) / np.sqrt(max(p0 * (1 - p0), 1e-6) / big["n"])
        flags = big.assign(z=z)[(z <= -z_thr) & (rate <= p0 - 0.08)]
        for run in merge_runs(flags, gap):
            n = run["n"].sum()
            findings.append(
                _incident(
                    dimension,
                    value,
                    "approval_rate",
                    run,
                    run["ok"].sum() / n,
                    p0,
                    run["z"].min(),
                    n,
                    hour,
                )
            )

        # Errors are rare and the median hour often has none, so the
        # overall error rate of this value is used as the expected one.
        err_rate = big["err"] / big["n"]
        e0 = max(
            float(err_rate.median()),
            float(g["err"].sum() / g["n"].sum()),
            0.002,
        )
        z = (err_rate - e0) / np.sqrt(e0 * (1 - e0) / big["n"])
        mask = (z >= z_thr) & (err_rate >= e0 + 0.05) & (big["err"] >= 5)
        flags = big.assign(z=z)[mask]
        for run in merge_runs(flags, gap):
            n = run["n"].sum()
            findings.append(
                _incident(
                    dimension,
                    value,
                    "error_rate",
                    run,
                    run["err"].sum() / n,
                    e0,
                    run["z"].max(),
                    n,
                    hour,
                )
            )

        # latency, robust z-score with median / MAD
        lat = big["lat"].astype(float)
        l0 = float(lat.median())
        mad = float((lat - l0).abs().median()) * 1.4826 or 1.0
        z = (lat - l0) / mad
        flags = big.assign(z=z)[(z >= z_thr) & (lat >= 1.5 * l0)]
        for run in merge_runs(flags, gap):
            avg = np.average(run["lat"], weights=run["n"])
            findings.append(
                _incident(
                    dimension,
                    value,
                    "latency",
                    run,
                    avg,
                    l0,
                    run["z"].max(),
                    run["n"].sum(),
                    hour,
                )
            )

        if dimension in ("merchant", "country") and value in counts.columns:
            findings += _volume_incidents(
                dimension,
                value,
                counts[value].to_numpy(dtype=float),
                totals,
                counts.index,
                z_thr,
            )
    return findings


def _volume_incidents(dimension, value, n, totals, hours, z_thr):
    if len(n) < 12:
        return []
    share0 = float(np.median(n / totals))
    expected = share0 * totals
    z = (n - expected) / np.sqrt(np.maximum(expected, 1e-9))
    ratio = n / np.maximum(expected, 1e-9)
    drop = (ratio <= 0.3) & (z <= -z_thr)
    spike = (ratio >= 3) & (z >= z_thr)
    mask = (expected >= 15) & (drop | spike)
    if not mask.any():
        return []

    flags = pd.DataFrame(
        {
            "t": hours[mask],
            "n": n[mask],
            "total": totals[mask],
            "z": z[mask],
        }
    )
    findings = []
    for run in merge_runs(flags, pd.Timedelta(hours=2)):
        share = run["n"].sum() / run["total"].sum()
        worst = run["z"].min() if share < share0 else run["z"].max()
        findings.append(
            _incident(
                dimension,
                value,
                "volume_share",
                run,
                share,
                share0,
                worst,
                run["n"].sum(),
                pd.Timedelta(hours=1),
            )
        )
    return findings


def detect_daily_refunds(df, z_thr=4.0, min_n=30):
    """Find refund waves. df columns: t (day), v (merchant), approved,
    refunds."""
    findings = []
    for value, g in df.groupby("v"):
        g = g[g["approved"] >= min_n].sort_values("t")
        if len(g) < 5:
            continue
        rate = g["refunds"] / g["approved"]
        r0 = max(float(rate.median()), 0.002)
        z = (rate - r0) / np.sqrt(r0 * (1 - r0) / g["approved"])
        flags = g.assign(z=z)[(z >= z_thr) & (rate >= max(3 * r0, r0 + 0.04))]
        for run in merge_runs(flags, pd.Timedelta(days=1)):
            n = run["approved"].sum()
            findings.append(
                _incident(
                    "merchant",
                    value,
                    "refund_rate",
                    run,
                    run["refunds"].sum() / n,
                    r0,
                    run["z"].max(),
                    n,
                    pd.Timedelta(days=1),
                )
            )
    return findings


# Root cause grouping


def effect_size(f):
    if f.metric in ("approval_rate", "error_rate"):
        return abs(f.observed - f.expected)
    return abs(math.log(max(f.observed, 1e-9) / max(f.expected, 1e-9)))


def _overlaps(a, b):
    # live findings don't have windows, they all cover "now"
    if a.window_start is None or b.window_start is None:
        return True
    return a.window_start < b.window_end and b.window_start < a.window_end


def _explains(root, child, root_dims):
    if root.dimension in root_dims:
        return True
    if child.metric == "volume_share":
        return effect_size(child) < effect_size(root)
    # a bigger slice with a similar effect explains a smaller one
    if root.sample_size < child.sample_size:
        return False
    return effect_size(root) >= 0.5 * effect_size(child)


def attribute_root_causes(findings):
    """Mark findings that are probably an echo of a bigger one.

    For example a drop in BR also shows up for a merchant whose customers
    are mostly from BR - that merchant gets correlated_with="country=BR".
    """
    by_metric = {}
    for f in findings:
        by_metric.setdefault(f.metric, []).append(f)

    for metric, group in by_metric.items():
        root_dims = ROOT_DIMENSIONS.get(metric, ())
        if metric == "volume_share":
            # sample_size is just the observed count here
            group = sorted(group, key=lambda f: -effect_size(f))
        else:
            # widest slices first, a merchant can't explain a provider
            group = sorted(
                group,
                key=lambda f: (f.dimension not in root_dims, -f.sample_size),
            )

        roots = []
        for f in group:
            parent = None
            for root in roots:
                if (
                    root.dimension != f.dimension
                    and _overlaps(root, f)
                    and _explains(root, f, root_dims)
                ):
                    parent = root
                    break
            if parent is None:
                roots.append(f)
            else:
                f.correlated_with = f"{parent.dimension}={parent.dim_value}"
                f.message += f" (likely explained by {f.correlated_with})"
    return findings


def _same_incident(found, truth):
    return (
        found["dimension"] == truth["dimension"]
        and found["dim_value"] == truth["dim_value"]
        and found["metric"] == truth["metric"]
        and found["window_start"] < truth["end_at"]
        and found["window_end"] > truth["start_at"]
    )


def match_ground_truth(truth, found):
    """Score batch findings against the anomalies injected by the seed.

    Findings that were grouped under another root cause are not counted.
    """
    correlated = [f for f in found if f.get("correlated_with")]
    found = [f for f in found if not f.get("correlated_with")]

    matched = set()
    rows = []
    for t in truth:
        hits = [i for i, f in enumerate(found) if _same_incident(f, t)]
        matched.update(hits)
        delay = None
        if hits:
            first = min(found[i]["window_start"] for i in hits)
            delay = (first - t["start_at"]).total_seconds() / 3600
        rows.append(
            {
                **t,
                "detected": bool(hits),
                "detection_delay_h": delay,
                "anomaly_ids": [found[i].get("anomaly_id") for i in hits],
            }
        )

    recall = None
    if rows:
        recall = sum(r["detected"] for r in rows) / len(rows)
    precision = len(matched) / len(found) if found else None
    unmatched = [f for i, f in enumerate(found) if i not in matched]
    return {
        "recall": recall,
        "precision": precision,
        "truth": rows,
        "unmatched": unmatched,
        "correlated": len(correlated),
    }
