from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .config import Settings
from .math_utils import clamp, normal_cdf
from .models import FairValue, MarketSpec, ReferencePrice, utc_now


SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0


@dataclass
class EwmaVolatilityEstimator:
    lambda_: float = 0.94
    sigma_floor: float = 0.20
    sigma_cap: float = 3.00

    def __post_init__(self) -> None:
        self._last_price: float | None = None
        self._last_ts: datetime | None = None
        daily_var = (self.sigma_floor / math.sqrt(365.0)) ** 2
        self._variance_per_second = daily_var / (24.0 * 60.0 * 60.0)

    def update(self, reference: ReferencePrice) -> float:
        price = float(reference.price)
        ts = reference.source_ts
        if price <= 0:
            return self.sigma
        if self._last_price is None or self._last_ts is None:
            self._last_price = price
            self._last_ts = ts
            return self.sigma

        dt = max(0.001, (ts - self._last_ts).total_seconds())
        log_return = math.log(price / self._last_price)
        realized_var_per_second = (log_return * log_return) / dt
        self._variance_per_second = (
            self.lambda_ * self._variance_per_second
            + (1.0 - self.lambda_) * realized_var_per_second
        )
        self._last_price = price
        self._last_ts = ts
        return self.sigma

    @property
    def sigma(self) -> float:
        annualized = math.sqrt(max(0.0, self._variance_per_second) * SECONDS_PER_YEAR)
        return clamp(annualized, self.sigma_floor, self.sigma_cap)


class LogReturnFairValueModel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.volatility = EwmaVolatilityEstimator(
            lambda_=settings.ewma_lambda,
            sigma_floor=settings.sigma_floor,
            sigma_cap=settings.sigma_cap,
        )

    def update_volatility(self, reference: ReferencePrice) -> float:
        return self.volatility.update(reference)

    def compute(
        self,
        market: MarketSpec,
        reference: ReferencePrice,
        now: datetime | None = None,
        sigma: float | None = None,
        drift_mu: float | None = None,
    ) -> FairValue | None:
        if market.start_price is None or market.start_price <= 0 or reference.price <= 0:
            return None

        current_time = now or utc_now()
        seconds_remaining = (market.end_ts - current_time).total_seconds()
        if seconds_remaining <= 0:
            return None

        sigma_value = clamp(sigma if sigma is not None else self.volatility.sigma, self.settings.sigma_floor, self.settings.sigma_cap)
        tau = seconds_remaining / SECONDS_PER_YEAR
        drift = self.settings.drift_mu if drift_mu is None else drift_mu
        denominator = sigma_value * math.sqrt(max(tau, 1e-12))
        numerator = math.log(float(reference.price / market.start_price)) + drift * tau
        z_score = numerator / denominator
        q_up_float = clamp(normal_cdf(z_score), 0.001, 0.999)
        q_up = Decimal(str(round(q_up_float, 6)))
        q_down = Decimal("1") - q_up

        return FairValue(
            market_id=market.market_id,
            q_up=q_up,
            q_down=q_down,
            sigma=sigma_value,
            drift_mu=drift,
            model_error=self.settings.model_error_buffer,
            computed_ts=current_time,
        )

