from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrategyRuntimeConfig(BaseModel):
    maker_margin: Decimal = Field(ge=0)
    maker_min_edge: Decimal = Field(ge=0)
    model_error_buffer: Decimal = Field(ge=0)
    slippage_buffer: Decimal = Field(ge=0)
    order_ttl_seconds: int = Field(gt=0)
    final_no_trade_seconds: int = Field(ge=0)


class RiskRuntimeConfig(BaseModel):
    base_order_size: Decimal = Field(gt=0)
    max_order_size: Decimal = Field(gt=0)
    max_position_per_market: Decimal = Field(gt=0)
    max_total_position: Decimal = Field(gt=0)
    max_daily_loss: Decimal = Field(gt=0)
    max_open_orders: int = Field(ge=0)


class PaperRuntimeConfig(BaseModel):
    paper_maker_fill_policy: Literal["none", "touch_after_quote_was_live"]
    paper_order_live_after_ms: int = Field(ge=0)


class RuntimeReadOnlyConfigStatus(BaseModel):
    execution_mode: Literal["paper", "live"]
    allow_live: bool
    live_requested: bool
    require_exact_resolution_source_for_live: bool
    enable_taker_orders: bool
    allow_emergency_account_cancel: bool
    require_api_auth: bool
    api_bearer_token_configured: bool
    polymarket_private_key_configured: bool
    azure_storage_configured: bool


class RuntimeConfig(BaseModel):
    strategy: StrategyRuntimeConfig
    risk: RiskRuntimeConfig
    paper: PaperRuntimeConfig
    read_only: RuntimeReadOnlyConfigStatus


class StrategyRuntimeConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maker_margin: Decimal | None = Field(default=None, ge=0)
    maker_min_edge: Decimal | None = Field(default=None, ge=0)
    model_error_buffer: Decimal | None = Field(default=None, ge=0)
    slippage_buffer: Decimal | None = Field(default=None, ge=0)
    order_ttl_seconds: int | None = Field(default=None, gt=0)
    final_no_trade_seconds: int | None = Field(default=None, ge=0)


class RiskRuntimeConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_order_size: Decimal | None = Field(default=None, gt=0)
    max_order_size: Decimal | None = Field(default=None, gt=0)
    max_position_per_market: Decimal | None = Field(default=None, gt=0)
    max_total_position: Decimal | None = Field(default=None, gt=0)
    max_daily_loss: Decimal | None = Field(default=None, gt=0)
    max_open_orders: int | None = Field(default=None, ge=0)


class PaperRuntimeConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_maker_fill_policy: Literal["none", "touch_after_quote_was_live"] | None = None
    paper_order_live_after_ms: int | None = Field(default=None, ge=0)


class RuntimeConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: StrategyRuntimeConfigPatch | None = None
    risk: RiskRuntimeConfigPatch | None = None
    paper: PaperRuntimeConfigPatch | None = None


class RuntimeConfigChangeRequest(BaseModel):
    config: RuntimeConfigPatch
    reason: str | None = None
    actor: str | None = None
    source: Literal["api", "ui", "automation"] = "api"
