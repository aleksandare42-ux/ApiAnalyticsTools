"""Provider selection for the gateway."""

import random
from collections import deque

# How hard smart routing punishes a provider with a low approval rate.
SMART_POWER = 8
# Degraded providers still get some traffic, otherwise we never see them
# recover.
EXPLORATION_FLOOR = 0.03


class HealthTracker:
    """Approval rate per provider over the last N attempts."""

    def __init__(self, size=300):
        self.size = size
        self.window = {}

    def record(self, provider, approved):
        if provider not in self.window:
            self.window[provider] = deque(maxlen=self.size)
        self.window[provider].append(1 if approved else 0)

    def rate(self, provider):
        w = self.window.get(provider)
        if not w or len(w) < 30:
            return None
        return sum(w) / len(w)

    def snapshot(self):
        result = {}
        for provider, w in self.window.items():
            result[provider] = {
                "approval_rate": self.rate(provider),
                "samples": len(w),
            }
        return result


def effective_weights(providers, method, health=None, smart=False):
    weights = {}
    for p in providers:
        if p["enabled"] and p["weight"] > 0 and method in p["methods"]:
            weights[p["provider_id"]] = float(p["weight"])

    if not (smart and health and weights):
        return weights

    rates = {pid: health.rate(pid) for pid in weights}
    known = [r for r in rates.values() if r is not None]
    if not known:
        return weights

    best = max(known) or 1.0
    total = sum(weights.values())
    for pid, r in rates.items():
        if r is None:
            continue
        penalized = weights[pid] * (r / best) ** SMART_POWER
        weights[pid] = max(penalized, EXPLORATION_FLOOR * total)
    return weights


def rank(weights, rng=random):
    """Order providers by weighted random draw without replacement.

    The first one is the primary route, the rest are cascade targets.
    """
    pool = dict(weights)
    order = []
    while pool:
        pid = rng.choices(list(pool), weights=list(pool.values()))[0]
        order.append(pid)
        del pool[pid]
    return order
