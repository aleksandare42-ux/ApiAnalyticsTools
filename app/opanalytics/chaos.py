"""Chaos rules - incidents we can inject into the simulators.

Rules live in the chaos_rules table, so every service sees the same set.

Effects:
    decline_rate  extra decline probability, 0..1 (providers)
    latency_ms    extra processing time in ms (providers)
    error_rate    probability of a timeout or 5xx (providers)
    refund_rate   extra refund probability for approved payments (traffic)
    volume_mult   multiplier for the target's share of traffic (traffic)
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone

TARGET_TYPES = ["global", "provider", "country", "merchant", "payment_method"]
EFFECTS = [
    "decline_rate",
    "latency_ms",
    "error_rate",
    "refund_rate",
    "volume_mult",
]

PRESETS = {
    "provider_decline_spike": {
        "name": "AlphaPay declines +30pp",
        "target_type": "provider",
        "target_value": "alphapay",
        "effect": "decline_rate",
        "value": 0.30,
        "reason": "do_not_honor",
        "description": "Provider A suddenly declines ~30% more payments "
        "(do_not_honor).",
    },
    "provider_latency": {
        "name": "BetaGate latency +1200ms",
        "target_type": "provider",
        "target_value": "betagate",
        "effect": "latency_ms",
        "value": 1200,
        "reason": None,
        "description": "Provider B gets slow, some payments start "
        "timing out.",
    },
    "merchant_refunds": {
        "name": "LuvMatch refund wave",
        "target_type": "merchant",
        "target_value": "m_017",
        "effect": "refund_rate",
        "value": 0.20,
        "reason": None,
        "description": "Merchant X gets an abnormal refund rate (+20pp).",
    },
    "country_low_success": {
        "name": "Brazil success collapse",
        "target_type": "country",
        "target_value": "BR",
        "effect": "decline_rate",
        "value": 0.35,
        "reason": "suspected_fraud",
        "description": "Country Y: issuers start declining as "
        "suspected_fraud.",
    },
    "provider_outage": {
        "name": "GammaPSP partial outage",
        "target_type": "provider",
        "target_value": "gammapsp",
        "effect": "error_rate",
        "value": 0.5,
        "reason": None,
        "description": "Half of GammaPSP calls time out or return 5xx. "
        "Cascading should save some of them.",
    },
    "merchant_volume_drop": {
        "name": "MelodyBox traffic drop",
        "target_type": "merchant",
        "target_value": "m_009",
        "effect": "volume_mult",
        "value": 0.05,
        "reason": None,
        "description": "Merchant integration is broken, its traffic "
        "almost disappears.",
    },
    "method_degradation": {
        "name": "Apple Pay 3DS failures",
        "target_type": "payment_method",
        "target_value": "apple_pay",
        "effect": "decline_rate",
        "value": 0.25,
        "reason": "authentication_failed",
        "description": "A wallet starts failing authentication on all "
        "providers.",
    },
}


@dataclass
class Rule:
    id: int
    name: str
    target_type: str
    target_value: str
    effect: str
    value: float
    reason: str = None
    active: bool = True
    expires_at: datetime = None

    def is_live(self, now=None):
        if not self.active:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > (now or datetime.now(timezone.utc))

    def matches(self, ctx):
        if self.target_type == "global":
            return True
        return ctx.get(self.target_type) == self.target_value


@dataclass
class Effects:
    decline_rate: float = 0.0
    latency_ms: float = 0.0
    error_rate: float = 0.0
    refund_rate: float = 0.0
    volume_mult: float = 1.0
    decline_reason: str = None


def aggregate(rules, ctx, now=None):
    """Sum up the effects of all live rules that match the context."""
    eff = Effects()
    for rule in rules:
        if not rule.is_live(now) or not rule.matches(ctx):
            continue

        if rule.effect == "volume_mult":
            eff.volume_mult *= rule.value
        else:
            current = getattr(eff, rule.effect)
            setattr(eff, rule.effect, current + rule.value)

        if rule.effect == "decline_rate" and rule.reason:
            if eff.decline_reason is None:
                eff.decline_reason = rule.reason

    eff.decline_rate = min(eff.decline_rate, 1.0)
    eff.error_rate = min(eff.error_rate, 1.0)
    return eff


def volume_multipliers(rules, target_type):
    result = {}
    for rule in rules:
        if not rule.is_live() or rule.effect != "volume_mult":
            continue
        if rule.target_type == target_type:
            old = result.get(rule.target_value, 1.0)
            result[rule.target_value] = old * rule.value
    return result


class ChaosCache:
    """Keeps live rules in memory and reloads them every `ttl` seconds."""

    def __init__(self, pool, ttl=2.0):
        self.pool = pool
        self.ttl = ttl
        self.rules = []
        self._loaded = 0.0

    async def get(self):
        if time.monotonic() - self._loaded > self.ttl:
            async with self.pool.connection() as conn:
                cur = await conn.execute("""
                    SELECT id, name, target_type, target_value, effect,
                           value, reason, active, expires_at
                    FROM chaos_rules
                    WHERE active
                      AND (expires_at IS NULL OR expires_at > now())
                    """)
                self.rules = [Rule(**row) for row in await cur.fetchall()]
            self._loaded = time.monotonic()
        return self.rules
