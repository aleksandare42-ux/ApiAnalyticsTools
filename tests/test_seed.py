from datetime import datetime, timezone

import pytest

from opanalytics import detection, seed


@pytest.fixture(scope="module")
def dataset():
    end = datetime(2026, 1, 31, tzinfo=timezone.utc)
    return seed.generate(rows=120_000, days=14, end=end, seed=7)


def in_window(df, anomaly):
    return (df.created_at >= anomaly["start_at"]) & (
        df.created_at < anomaly["end_at"]
    )


def hourly(df, column):
    df = df.assign(
        t=df.created_at.dt.floor("h"),
        v=df[column],
        ok=df.status.eq("approved"),
        err=df.status.eq("error"),
        lat=df.processing_time_ms.where(df.status.ne("error")),
    )
    grouped = df.groupby(["t", "v"]).agg(
        n=("ok", "size"),
        ok=("ok", "sum"),
        err=("err", "sum"),
        lat=("lat", "mean"),
    )
    return grouped.reset_index()


def daily_refunds(df):
    df = df.assign(
        t=df.created_at.dt.floor("D"),
        v=df.merchant_id,
        approved=df.status.eq("approved"),
    )
    grouped = df.groupby(["t", "v"]).agg(
        approved=("approved", "sum"),
        refunds=("refunded", "sum"),
    )
    return grouped.reset_index()


def test_dataset_looks_valid(dataset):
    df, truth = dataset
    assert len(df) > 110_000
    assert list(df.columns) == seed.COLUMNS
    assert set(df.status) == {"approved", "declined", "error"}

    approved = df.status == "approved"
    assert df.loc[approved, "decline_reason"].isna().all()
    assert df.loc[~approved, "decline_reason"].notna().all()
    assert (df.loc[df.refunded, "status"] == "approved").all()
    assert 0.80 < approved.mean() < 0.92
    assert len(truth) == len(seed.SEED_ANOMALIES)


def test_anomalies_are_in_the_data(dataset):
    df, truth = dataset
    anomalies = {a["name"]: a for a in truth}

    spike = anomalies["AlphaPay decline spike"]
    alphapay = df.provider == "alphapay"
    inside = df[alphapay & in_window(df, spike)]
    outside = df[alphapay & ~in_window(df, spike)]
    drop = (outside.status == "approved").mean() - (
        inside.status == "approved"
    ).mean()
    assert drop > 0.2

    slow = anomalies["BetaGate latency degradation"]
    betagate = df[(df.provider == "betagate") & in_window(df, slow)]
    assert betagate.processing_time_ms.mean() > 900


def test_detector_finds_injected_anomalies(dataset):
    df, truth = dataset
    findings = []
    for dimension, column in detection.DIMENSIONS.items():
        findings += detection.detect_hourly(hourly(df, column), dimension)
    findings += detection.detect_daily_refunds(daily_refunds(df))
    detection.attribute_root_causes(findings)

    found = []
    for i, finding in enumerate(findings):
        found.append({**finding.as_dict(), "anomaly_id": i})
    result = detection.match_ground_truth(truth, found)

    missed = [t["name"] for t in result["truth"] if not t["detected"]]
    assert result["recall"] >= 5 / 6, f"missed: {missed}"
    noise = [f["message"] for f in result["unmatched"]]
    assert result["precision"] >= 0.5, f"too many false alarms: {noise}"
