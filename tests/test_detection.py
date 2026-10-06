import pytest

from opanalytics import detection
from opanalytics.detection import Finding


def window_row(**kwargs):
    row = {
        "v": "alphapay",
        "cur_n": 1000,
        "cur_ok": 880,
        "cur_err": 4,
        "cur_lat": 350,
        "cur_lat_sd": 120,
        "base_n": 10000,
        "base_ok": 8800,
        "base_err": 40,
        "base_lat": 350,
        "base_lat_sd": 120,
    }
    row.update(kwargs)
    return row


def test_two_prop_z():
    assert detection.two_prop_z(50, 100, 80, 100) < -4
    assert detection.two_prop_z(80, 100, 80, 100) == pytest.approx(0)


def test_normal_window():
    assert detection.check_window_row("provider", window_row()) == []


def test_approval_drop():
    findings = detection.check_window_row("provider", window_row(cur_ok=600))
    assert [f.metric for f in findings] == ["approval_rate"]
    assert findings[0].severity == "critical"


def test_latency_and_errors():
    row = window_row(cur_lat=1500, cur_err=120)
    metrics = {f.metric for f in detection.check_window_row("provider", row)}
    assert {"latency", "error_rate"} <= metrics


def test_small_sample_is_ignored():
    row = window_row(cur_n=20, cur_ok=2)
    assert detection.check_window_row("provider", row) == []


def test_refunds():
    row = {
        "v": "m_017",
        "cur_ok": 500,
        "cur_ref": 100,
        "base_ok": 5000,
        "base_ref": 100,
    }
    assert detection.check_refund_row(row)

    row["cur_ref"] = 11
    assert not detection.check_refund_row(row)


def test_volume_drop():
    rows = [{"v": f"m{i}", "cur_n": 200, "base_n": 2000} for i in range(10)]
    rows.append({"v": "drop", "cur_n": 5, "base_n": 2000})
    findings = detection.check_volume_rows("merchant", rows)
    assert [f.dim_value for f in findings] == ["drop"]


def test_root_cause_by_provider():
    provider = Finding("provider", "betagate", "latency", 1300, 410, 50, 5000)
    merchant = Finding("merchant", "m_001", "latency", 700, 400, 8, 300)
    detection.attribute_root_causes([merchant, provider])
    assert provider.correlated_with is None
    assert merchant.correlated_with == "provider=betagate"


def test_root_cause_by_country():
    country = Finding("country", "BR", "approval_rate", 0.6, 0.85, -20, 900)
    merchant = Finding(
        "merchant", "m_012", "approval_rate", 0.75, 0.85, -8, 300
    )
    detection.attribute_root_causes([merchant, country])
    assert country.correlated_with is None
    assert merchant.correlated_with == "country=BR"


def test_small_slice_does_not_explain_provider():
    provider = Finding(
        "provider", "alphapay", "approval_rate", 0.71, 0.87, -5, 560
    )
    merchant = Finding(
        "merchant", "m_001", "approval_rate", 0.70, 0.88, -6, 90
    )
    detection.attribute_root_causes([merchant, provider])
    assert provider.correlated_with is None
    assert merchant.correlated_with == "provider=alphapay"
