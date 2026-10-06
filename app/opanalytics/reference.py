"""Reference data and the simple model of how payments behave.

Both the historical generator (seed.py) and the live simulators use these
functions, so seeded and live data look the same.
"""

import math
import random
from dataclasses import dataclass

import numpy as np

PROVIDERS = [
    {
        "provider_id": "alphapay",
        "name": "AlphaPay",
        "base_approval": 0.90,
        "base_latency_ms": 320,
        "methods": ["card", "apple_pay", "google_pay"],
        "fee_pct": 2.4,
        "weight": 35,
    },
    {
        "provider_id": "betagate",
        "name": "BetaGate",
        "base_approval": 0.87,
        "base_latency_ms": 410,
        "methods": ["card", "apple_pay", "google_pay"],
        "fee_pct": 2.1,
        "weight": 30,
    },
    {
        "provider_id": "gammapsp",
        "name": "GammaPSP",
        "base_approval": 0.84,
        "base_latency_ms": 560,
        "methods": ["card", "paypal"],
        "fee_pct": 1.9,
        "weight": 20,
    },
    {
        "provider_id": "deltaacq",
        "name": "DeltaAcq",
        "base_approval": 0.88,
        "base_latency_ms": 280,
        "methods": ["card", "open_banking"],
        "fee_pct": 1.6,
        "weight": 15,
    },
]
PROVIDER_BY_ID = {p["provider_id"]: p for p in PROVIDERS}

# country: (currency, share of traffic, approval factor)
COUNTRIES = {
    "US": ("USD", 0.22, 1.00),
    "GB": ("GBP", 0.09, 1.00),
    "DE": ("EUR", 0.08, 1.01),
    "FR": ("EUR", 0.06, 0.99),
    "ES": ("EUR", 0.04, 0.98),
    "IT": ("EUR", 0.04, 0.97),
    "NL": ("EUR", 0.03, 1.01),
    "PL": ("PLN", 0.03, 0.99),
    "UA": ("UAH", 0.03, 0.93),
    "BR": ("BRL", 0.07, 0.92),
    "MX": ("MXN", 0.05, 0.92),
    "IN": ("INR", 0.05, 0.88),
    "TR": ("TRY", 0.04, 0.90),
    "CA": ("CAD", 0.05, 1.00),
    "AU": ("AUD", 0.04, 1.00),
    "JP": ("JPY", 0.03, 0.97),
    "SE": ("SEK", 0.02, 1.01),
}
COUNTRY_CODES = list(COUNTRIES)
COUNTRY_WEIGHTS = np.array([COUNTRIES[c][1] for c in COUNTRY_CODES])
COUNTRY_WEIGHTS = COUNTRY_WEIGHTS / COUNTRY_WEIGHTS.sum()
OPEN_BANKING_COUNTRIES = {"GB", "DE", "FR", "ES", "IT", "NL", "PL", "SE"}

# currency units per 1 USD
FX = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "PLN": 3.95,
    "UAH": 41.5,
    "BRL": 5.4,
    "MXN": 18.5,
    "INR": 83.5,
    "TRY": 34.0,
    "CAD": 1.36,
    "AUD": 1.52,
    "JPY": 149.0,
    "SEK": 10.6,
}

# method: (share of traffic, approval factor, latency factor)
METHODS = {
    "card": (0.66, 1.00, 1.0),
    "apple_pay": (0.14, 1.04, 0.9),
    "google_pay": (0.11, 1.03, 0.9),
    "paypal": (0.06, 1.02, 1.4),
    "open_banking": (0.03, 0.97, 1.8),
}
METHOD_CODES = list(METHODS)

# vertical: (median ticket in USD, sigma of the lognormal)
VERTICALS = {
    "dating": (25, 0.6),
    "gaming": (12, 0.9),
    "streaming": (11, 0.3),
    "edtech": (40, 0.7),
    "saas": (49, 0.8),
    "ecommerce": (65, 0.9),
    "travel": (320, 0.8),
    "fitness": (20, 0.5),
}
RISK_FACTOR = {"low": 1.0, "medium": 0.97, "high": 0.93}

MERCHANT_FIELDS = [
    "merchant_id",
    "name",
    "vertical",
    "home_country",
    "risk_tier",
    "volume_weight",
    "base_refund_rate",
]
MERCHANTS = [
    dict(zip(MERCHANT_FIELDS, row))
    for row in [
        ("m_001", "Flicker TV", "streaming", "US", "low", 3.0, 0.010),
        ("m_002", "Pixel Legends", "gaming", "US", "medium", 2.6, 0.015),
        ("m_003", "HeartSync", "dating", "GB", "medium", 1.8, 0.030),
        ("m_004", "LinguaPro", "edtech", "DE", "low", 1.4, 0.020),
        ("m_005", "CloudNotes", "saas", "US", "low", 1.6, 0.012),
        ("m_006", "Trendora", "ecommerce", "FR", "medium", 1.5, 0.025),
        ("m_007", "SkyHop Travel", "travel", "GB", "medium", 0.6, 0.030),
        ("m_008", "FitPulse", "fitness", "US", "low", 1.3, 0.015),
        ("m_009", "MelodyBox", "streaming", "SE", "low", 1.7, 0.010),
        ("m_010", "QuestRealm", "gaming", "PL", "medium", 1.2, 0.015),
        ("m_011", "CodeCamp+", "edtech", "UA", "low", 0.8, 0.020),
        ("m_012", "Shoply", "ecommerce", "BR", "high", 1.1, 0.030),
        ("m_013", "MatchMe", "dating", "US", "medium", 1.9, 0.030),
        ("m_014", "VPNShield", "saas", "NL", "medium", 1.0, 0.020),
        ("m_015", "YogaFlow", "fitness", "ES", "low", 0.7, 0.015),
        ("m_016", "StoryLane", "streaming", "IT", "low", 0.9, 0.010),
        ("m_017", "LuvMatch", "dating", "MX", "high", 1.2, 0.025),
        ("m_018", "BrainBoost", "edtech", "IN", "medium", 0.9, 0.020),
        ("m_019", "TripNest", "travel", "DE", "medium", 0.5, 0.030),
        ("m_020", "ArenaX", "gaming", "TR", "high", 1.0, 0.020),
        ("m_021", "PhotoForge", "saas", "CA", "low", 0.8, 0.012),
        ("m_022", "Sneakr", "ecommerce", "AU", "medium", 0.7, 0.025),
        ("m_023", "AnimeVerse", "streaming", "JP", "low", 0.8, 0.010),
        ("m_024", "Habitly", "fitness", "GB", "low", 0.6, 0.015),
    ]
]
MERCHANT_BY_ID = {m["merchant_id"]: m for m in MERCHANTS}
MERCHANT_IDS = [m["merchant_id"] for m in MERCHANTS]
MERCHANT_WEIGHTS = np.array([m["volume_weight"] for m in MERCHANTS])
MERCHANT_WEIGHTS = MERCHANT_WEIGHTS / MERCHANT_WEIGHTS.sum()

# Share of a merchant's payments that come from its home country.
HOME_COUNTRY_SHARE = 0.4

DECLINE_REASONS = {
    "insufficient_funds": 0.34,
    "do_not_honor": 0.22,
    "suspected_fraud": 0.10,
    "authentication_failed": 0.09,
    "expired_card": 0.07,
    "limit_exceeded": 0.07,
    "invalid_cvv": 0.06,
    "card_not_supported": 0.03,
    "issuer_unavailable": 0.02,
}
DECLINE_CODES = list(DECLINE_REASONS)
DECLINE_WEIGHTS = np.array(list(DECLINE_REASONS.values()))
DECLINE_WEIGHTS = DECLINE_WEIGHTS / DECLINE_WEIGHTS.sum()
ERROR_REASONS = ["provider_timeout", "provider_unavailable"]

# Declines that make sense to retry on another provider.
SOFT_DECLINES = {
    "do_not_honor",
    "issuer_unavailable",
    "provider_timeout",
    "provider_unavailable",
}

BASE_ERROR_RATE = 0.004
LATENCY_SIGMA = 0.35

# Relative traffic by hour of day (UTC), peak in the evening.
HOURLY_PROFILE = np.array([
    0.55, 0.45, 0.38, 0.34, 0.33, 0.36, 0.45, 0.60,
    0.75, 0.85, 0.92, 0.97, 1.00, 1.02, 1.04, 1.06,
    1.10, 1.18, 1.28, 1.35, 1.32, 1.18, 0.95, 0.72,
])  # fmt: skip


def method_weights(country):
    weights = np.array([METHODS[m][0] for m in METHOD_CODES])
    if country not in OPEN_BANKING_COUNTRIES:
        weights[METHOD_CODES.index("open_banking")] = 0
    return weights / weights.sum()


def approval_prob(
    provider_base, country_factor, method_factor, risk_factor, amount_usd
):
    """Probability that a payment is approved.

    Works with plain floats and with numpy arrays (used by the generator).
    Big tickets are approved a bit less often.
    """
    amount_factor = 1 - 0.04 * np.log10(np.maximum(amount_usd, 1) / 50)
    amount_factor = np.clip(amount_factor, 0.9, 1.03)
    p = (
        provider_base
        * country_factor
        * method_factor
        * risk_factor
        * amount_factor
    )
    return np.clip(p, 0.01, 0.995)


def latency_mu(base_latency_ms, latency_factor):
    # mu of a lognormal whose mean is base * factor
    mean = np.asarray(base_latency_ms) * latency_factor
    return np.log(mean) - LATENCY_SIGMA**2 / 2


def sample_latency_ms(base_latency_ms, method, rng=random):
    mu = float(latency_mu(base_latency_ms, METHODS[method][2]))
    return math.exp(rng.gauss(mu, LATENCY_SIGMA))


def sample_amount_usd(vertical, rng=random):
    median, sigma = VERTICALS[vertical]
    amount = math.exp(rng.gauss(math.log(median), sigma))
    return round(max(0.99, amount), 2)


def to_local(amount_usd, currency):
    return round(amount_usd * FX[currency], 2)


def sample_decline_reason(rng=random):
    return rng.choices(DECLINE_CODES, weights=DECLINE_WEIGHTS)[0]


@dataclass
class SimPayment:
    merchant_id: str
    amount: float
    currency: str
    country: str
    payment_method: str


def random_payment(rng=random, merchant_mult=None, country_mult=None):
    """Make one realistic payment request.

    merchant_mult / country_mult change the traffic mix, they come from
    `volume_mult` chaos rules.
    """
    merchant_mult = merchant_mult or {}
    country_mult = country_mult or {}

    weights = [
        w * merchant_mult.get(m, 1.0)
        for m, w in zip(MERCHANT_IDS, MERCHANT_WEIGHTS)
    ]
    merchant_id = rng.choices(MERCHANT_IDS, weights=weights)[0]
    merchant = MERCHANT_BY_ID[merchant_id]

    home = merchant["home_country"]
    if rng.random() < HOME_COUNTRY_SHARE and country_mult.get(home, 1) > 0:
        country = home
    else:
        weights = [
            w * country_mult.get(c, 1.0)
            for c, w in zip(COUNTRY_CODES, COUNTRY_WEIGHTS)
        ]
        country = rng.choices(COUNTRY_CODES, weights=weights)[0]

    currency = COUNTRIES[country][0]
    method = rng.choices(METHOD_CODES, weights=method_weights(country))[0]
    amount_usd = sample_amount_usd(merchant["vertical"], rng)
    return SimPayment(
        merchant_id=merchant_id,
        amount=to_local(amount_usd, currency),
        currency=currency,
        country=country,
        payment_method=method,
    )
