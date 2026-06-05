from decimal import Decimal

from polyedge.math_utils import crypto_taker_fee_per_share, floor_to_tick


def test_crypto_taker_fee_per_share_midpoint() -> None:
    assert crypto_taker_fee_per_share(Decimal("0.50")) == Decimal("0.017500")


def test_crypto_taker_fee_per_share_off_midpoint() -> None:
    assert crypto_taker_fee_per_share(Decimal("0.52")) == Decimal("0.017472")


def test_floor_to_tick() -> None:
    assert floor_to_tick(Decimal("0.527"), Decimal("0.01")) == Decimal("0.52")

