from datetime import datetime, timedelta, timezone

import pytest

from opanalytics import chaos


def make_rule(**kwargs):
    fields = {
        "id": 1,
        "name": "test",
        "target_type": "provider",
        "target_value": "alphapay",
        "effect": "decline_rate",
        "value": 0.3,
    }
    fields.update(kwargs)
    return chaos.Rule(**fields)


def test_aggregate():
    rules = [
        make_rule(),
        make_rule(id=2, target_type="country", target_value="BR", value=0.2),
        make_rule(
            id=3,
            target_type="global",
            target_value=None,
            effect="latency_ms",
            value=100,
        ),
    ]

    effects = chaos.aggregate(rules, {"provider": "alphapay", "country": "BR"})
    assert effects.decline_rate == pytest.approx(0.5)
    assert effects.latency_ms == 100

    effects = chaos.aggregate(rules, {"provider": "betagate", "country": "US"})
    assert effects.decline_rate == 0
    assert effects.latency_ms == 100


def test_inactive_and_expired_rules_are_ignored():
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    ctx = {"provider": "alphapay"}
    assert chaos.aggregate([make_rule(expires_at=past)], ctx).decline_rate == 0
    assert chaos.aggregate([make_rule(active=False)], ctx).decline_rate == 0


def test_presets_are_valid():
    for preset in chaos.PRESETS.values():
        assert preset["target_type"] in chaos.TARGET_TYPES
        assert preset["effect"] in chaos.EFFECTS
