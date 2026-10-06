import random

from opanalytics import reference


def test_approval_prob_is_lower_for_big_amounts():
    small = reference.approval_prob(0.9, 1.0, 1.0, 1.0, 10)
    big = reference.approval_prob(0.9, 1.0, 1.0, 1.0, 5000)
    assert 0.01 <= big < small <= 0.995


def test_random_payment():
    rng = random.Random(1)
    for _ in range(2000):
        payment = reference.random_payment(rng)
        assert payment.merchant_id in reference.MERCHANT_BY_ID
        assert payment.currency == reference.COUNTRIES[payment.country][0]
        assert payment.amount > 0
        if payment.payment_method == "open_banking":
            assert payment.country in reference.OPEN_BANKING_COUNTRIES


def test_zero_volume_removes_merchant():
    rng = random.Random(2)
    merchants = set()
    for _ in range(3000):
        payment = reference.random_payment(rng, merchant_mult={"m_001": 0})
        merchants.add(payment.merchant_id)
    assert "m_001" not in merchants
