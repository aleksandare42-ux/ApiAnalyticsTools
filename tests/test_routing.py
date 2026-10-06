import random

from opanalytics import reference, routing

PROVIDERS = [dict(p, enabled=True) for p in reference.PROVIDERS]


def alphapay_share(weights):
    return weights["alphapay"] / sum(weights.values())


def test_only_enabled_providers_with_the_method():
    assert list(routing.effective_weights(PROVIDERS, "paypal")) == ["gammapsp"]

    providers = [dict(p) for p in PROVIDERS]
    providers[0]["enabled"] = False
    assert "alphapay" not in routing.effective_weights(providers, "card")


def test_rank_returns_every_provider_once():
    order = routing.rank({"a": 1, "b": 2, "c": 3}, random.Random(0))
    assert sorted(order) == ["a", "b", "c"]


def test_smart_routing_moves_traffic_from_bad_provider():
    health = routing.HealthTracker()
    for i in range(200):
        health.record("alphapay", i % 2 == 0)  # 50% approved
        for name in ("betagate", "gammapsp", "deltaacq"):
            health.record(name, i % 10 != 0)  # 90% approved

    plain = routing.effective_weights(PROVIDERS, "card", health, smart=False)
    smart = routing.effective_weights(PROVIDERS, "card", health, smart=True)
    assert alphapay_share(smart) < alphapay_share(plain) / 3
