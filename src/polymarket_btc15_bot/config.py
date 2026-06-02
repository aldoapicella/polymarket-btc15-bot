from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "polymarket-btc15-bot"
    execution_mode: Literal["paper", "live"] = "paper"
    require_api_auth: bool = False
    api_bearer_token: str | None = None

    allow_live: bool = False
    confirm_non_restricted_location: bool = False
    require_exact_resolution_source_for_live: bool = True

    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_rtds_url: str = "wss://ws-live-data.polymarket.com"
    polymarket_private_key: str | None = None
    polymarket_funder: str | None = None
    polymarket_signature_type: int = 1
    polymarket_chain_id: int = 137

    chainlink_btc_usd_url: str | None = None
    chainlink_api_key: str | None = None
    chainlink_data_streams_api_url: str = "https://api.dataengine.chain.link"
    chainlink_data_streams_api_key: str | None = None
    chainlink_data_streams_api_secret: str | None = None
    chainlink_btc_usd_feed_id: str | None = None
    chainlink_btc_usd_product_url: str = "https://data.chain.link/streams/btc-usd"
    chainlink_btc_usd_product_name: str = "BTC/USD-RefPrice-DS-Premium-Global-003"
    chainlink_btc_usd_feed_id_suffix: str = "75b8"

    target_asset: str = "BTC"
    target_horizon: str = "15m"
    discovery_limit: int = 250
    discovery_interval_seconds: float = 20.0
    enable_polymarket_rtds_chainlink: bool = True
    enable_polymarket_rtds_binance: bool = True
    start_price_capture_grace_seconds: float = 5.0

    base_order_size: Decimal = Decimal("5")
    max_order_size: Decimal = Decimal("5")
    max_position_per_market: Decimal = Decimal("25")
    max_total_position: Decimal = Decimal("100")
    max_daily_loss: Decimal = Decimal("50")
    max_open_orders: int = 8

    taker_min_edge: Decimal = Decimal("0.03")
    enable_taker_orders: bool = False
    maker_min_edge: Decimal = Decimal("0.01")
    maker_margin: Decimal = Decimal("0.015")
    adverse_selection_buffer: Decimal = Decimal("0.005")
    model_error_buffer: Decimal = Decimal("0.01")
    slippage_buffer: Decimal = Decimal("0.002")

    ewma_lambda: float = 0.94
    sigma_floor: float = 0.20
    sigma_cap: float = 3.00
    drift_mu: float = 0.0

    max_reference_age_ms: int = 1500
    rtds_ping_interval_seconds: float = 5.0
    reference_divergence_pause_threshold: Decimal = Decimal("0.0015")
    max_book_age_ms: int = 1500
    final_no_trade_seconds: int = 30
    order_ttl_seconds: int = 10
    paper_maker_fill_policy: Literal["none", "touch_after_quote_was_live"] = "touch_after_quote_was_live"
    paper_order_live_after_ms: int = 250
    allow_emergency_account_cancel: bool = False
    enable_live_heartbeat: bool = True
    live_heartbeat_interval_seconds: float = 5.0
    live_heartbeat_failure_threshold: int = 2

    kill_switch_file: Path = Path("data/KILL_SWITCH")
    recorder_path: Path = Path("data/events.jsonl")
    azure_storage_account_name: str | None = None
    azure_storage_container_name: str = "bot-events"
    azure_storage_table_name: str = "BotEventIndex"
    azure_event_index_types: str = "market,market_start_price,fair_value,decision,execution_report,feed_error,reference"
    azure_recorder_batch_max_events: int = 1000
    azure_recorder_batch_max_bytes: int = 524288
    azure_recorder_flush_interval_seconds: float = 2.0
    azure_recorder_queue_max_events: int = 100000
    azure_recorder_flush_retries: int = 3
    run_bot_on_startup: bool = False

    @field_validator("target_asset")
    @classmethod
    def normalize_asset(cls, value: str) -> str:
        return value.upper()

    @field_validator("target_horizon")
    @classmethod
    def normalize_horizon(cls, value: str) -> str:
        return value.lower()

    @property
    def live_requested(self) -> bool:
        return self.execution_mode == "live"

    @property
    def exact_resolution_source_configured(self) -> bool:
        return bool(
            self.chainlink_btc_usd_feed_id
            and self.chainlink_data_streams_api_key
            and self.chainlink_data_streams_api_secret
        )

    @property
    def azure_event_index_type_set(self) -> set[str]:
        return {
            item.strip()
            for item in self.azure_event_index_types.split(",")
            if item.strip()
        }


def load_settings() -> Settings:
    return Settings()
