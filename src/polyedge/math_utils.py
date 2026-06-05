from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import erf, sqrt


ONE = Decimal("1")
ZERO = Decimal("0")


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def crypto_taker_fee_per_share(price: Decimal) -> Decimal:
    if price < ZERO or price > ONE:
        raise ValueError("price must be between 0 and 1")
    return Decimal("0.07") * price * (ONE - price)


def floor_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= ZERO:
        raise ValueError("tick_size must be positive")
    ticks = (price / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    return ticks * tick_size


def ceil_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= ZERO:
        raise ValueError("tick_size must be positive")
    ticks = (price / tick_size).to_integral_value(rounding=ROUND_CEILING)
    return ticks * tick_size


def clamp_probability_decimal(value: Decimal) -> Decimal:
    return max(Decimal("0.001"), min(Decimal("0.999"), value))

