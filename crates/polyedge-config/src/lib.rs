use polyedge_domain::decimal_string;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::env;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("live trading is blocked: {0}")]
    LiveBlocked(String),
    #[error("invalid decimal for {name}: {value}")]
    InvalidDecimal { name: String, value: String },
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionMode {
    #[default]
    Paper,
    Live,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DeployConfig {
    pub app_name: String,
    pub run_bot_on_startup: bool,
    pub require_api_auth: bool,
    pub rust_proxy_runtime_api: bool,
    pub rust_upstream_api_base_url: Option<String>,
    pub rust_upstream_ws_url: Option<String>,
    #[serde(skip_serializing)]
    pub api_bearer_token: Option<String>,
}

impl Default for DeployConfig {
    fn default() -> Self {
        Self {
            app_name: "polyedge".to_owned(),
            run_bot_on_startup: false,
            require_api_auth: false,
            rust_proxy_runtime_api: false,
            rust_upstream_api_base_url: None,
            rust_upstream_ws_url: None,
            api_bearer_token: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TargetConfig {
    pub polymarket_gamma_url: String,
    pub polymarket_clob_url: String,
    pub polymarket_ws_url: String,
    pub polymarket_rtds_url: String,
    pub chainlink_reference_url: Option<String>,
    #[serde(skip_serializing)]
    pub chainlink_api_key: Option<String>,
    pub asset: String,
    pub asset_name: String,
    pub horizon: String,
    pub resolution_source: String,
    pub chainlink_symbol: String,
    pub binance_symbol: String,
    pub coinbase_product_id: String,
    pub discovery_limit: usize,
    pub discovery_interval_seconds: f64,
    pub enable_polymarket_rtds_chainlink: bool,
    pub enable_polymarket_rtds_binance: bool,
    pub rtds_ping_interval_seconds: f64,
    pub start_price_capture_grace_seconds: f64,
    #[serde(with = "decimal_string")]
    pub reference_divergence_pause_threshold: Decimal,
}

impl Default for TargetConfig {
    fn default() -> Self {
        Self {
            polymarket_gamma_url: "https://gamma-api.polymarket.com".to_owned(),
            polymarket_clob_url: "https://clob.polymarket.com".to_owned(),
            polymarket_ws_url: "wss://ws-subscriptions-clob.polymarket.com/ws/market".to_owned(),
            polymarket_rtds_url: "wss://ws-live-data.polymarket.com".to_owned(),
            chainlink_reference_url: None,
            chainlink_api_key: None,
            asset: "BTC".to_owned(),
            asset_name: "Bitcoin".to_owned(),
            horizon: "15m".to_owned(),
            resolution_source: "chainlink_reference".to_owned(),
            chainlink_symbol: "btc/usd".to_owned(),
            binance_symbol: "btcusdt".to_owned(),
            coinbase_product_id: "BTC-USD".to_owned(),
            discovery_limit: 250,
            discovery_interval_seconds: 20.0,
            enable_polymarket_rtds_chainlink: true,
            enable_polymarket_rtds_binance: true,
            rtds_ping_interval_seconds: 5.0,
            start_price_capture_grace_seconds: 5.0,
            reference_divergence_pause_threshold: Decimal::new(15, 4),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StrategyConfig {
    #[serde(with = "decimal_string")]
    pub taker_min_edge: Decimal,
    pub enable_taker_orders: bool,
    #[serde(with = "decimal_string")]
    pub maker_min_edge: Decimal,
    #[serde(with = "decimal_string")]
    pub maker_margin: Decimal,
    #[serde(with = "decimal_string")]
    pub adverse_selection_buffer: Decimal,
    #[serde(with = "decimal_string")]
    pub model_error_buffer: Decimal,
    #[serde(with = "decimal_string")]
    pub slippage_buffer: Decimal,
    pub ewma_lambda: f64,
    pub sigma_floor: f64,
    pub sigma_cap: f64,
    pub drift_mu: f64,
    pub final_no_trade_seconds: i64,
    pub order_ttl_seconds: i64,
}

impl Default for StrategyConfig {
    fn default() -> Self {
        Self {
            taker_min_edge: Decimal::new(3, 2),
            enable_taker_orders: false,
            maker_min_edge: Decimal::new(1, 2),
            maker_margin: Decimal::new(15, 3),
            adverse_selection_buffer: Decimal::new(5, 3),
            model_error_buffer: Decimal::new(1, 2),
            slippage_buffer: Decimal::new(2, 3),
            ewma_lambda: 0.94,
            sigma_floor: 0.20,
            sigma_cap: 3.00,
            drift_mu: 0.0,
            final_no_trade_seconds: 30,
            order_ttl_seconds: 10,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RiskConfig {
    #[serde(with = "decimal_string")]
    pub base_order_size: Decimal,
    #[serde(with = "decimal_string")]
    pub max_order_size: Decimal,
    #[serde(with = "decimal_string")]
    pub max_position_per_market: Decimal,
    #[serde(with = "decimal_string")]
    pub max_total_position: Decimal,
    #[serde(with = "decimal_string")]
    pub max_daily_loss: Decimal,
    pub max_open_orders: usize,
    pub max_reference_age_ms: i64,
    pub max_book_age_ms: i64,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            base_order_size: Decimal::from(5),
            max_order_size: Decimal::from(5),
            max_position_per_market: Decimal::from(25),
            max_total_position: Decimal::from(100),
            max_daily_loss: Decimal::from(50),
            max_open_orders: 8,
            max_reference_age_ms: 1500,
            max_book_age_ms: 1500,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PaperConfig {
    pub maker_fill_policy: String,
    pub order_live_after_ms: i64,
}

impl Default for PaperConfig {
    fn default() -> Self {
        Self {
            maker_fill_policy: "touch_after_quote_was_live".to_owned(),
            order_live_after_ms: 250,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LiveConfig {
    pub execution_mode: ExecutionMode,
    pub allow_live: bool,
    pub confirm_non_restricted_location: bool,
    pub require_exact_resolution_source_for_live: bool,
    #[serde(skip_serializing)]
    pub polymarket_private_key: Option<String>,
    pub polymarket_funder: Option<String>,
    pub allow_emergency_account_cancel: bool,
    pub enable_heartbeat: bool,
    pub heartbeat_interval_seconds: f64,
    pub heartbeat_failure_threshold: usize,
}

impl Default for LiveConfig {
    fn default() -> Self {
        Self {
            execution_mode: ExecutionMode::Paper,
            allow_live: false,
            confirm_non_restricted_location: false,
            require_exact_resolution_source_for_live: true,
            polymarket_private_key: None,
            polymarket_funder: None,
            allow_emergency_account_cancel: false,
            enable_heartbeat: true,
            heartbeat_interval_seconds: 5.0,
            heartbeat_failure_threshold: 2,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AzureConfig {
    pub storage_account_name: Option<String>,
    pub storage_container_name: String,
    pub storage_table_name: String,
    pub chart_table_name: String,
    pub market_table_name: String,
}

impl Default for AzureConfig {
    fn default() -> Self {
        Self {
            storage_account_name: None,
            storage_container_name: "bot-events".to_owned(),
            storage_table_name: "BotEventIndex".to_owned(),
            chart_table_name: "BotChartSeries".to_owned(),
            market_table_name: "BotMarketCatalog".to_owned(),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct RuntimeSettings {
    pub deploy: DeployConfig,
    pub target: TargetConfig,
    pub strategy: StrategyConfig,
    pub risk: RiskConfig,
    pub paper: PaperConfig,
    pub live: LiveConfig,
    pub azure: AzureConfig,
}

impl RuntimeSettings {
    pub fn from_env() -> Result<Self, ConfigError> {
        let mut settings = Self::default();
        if let Ok(app_name) = env::var("APP_NAME") {
            settings.deploy.app_name = app_name;
        }
        settings.deploy.run_bot_on_startup =
            env_bool("RUN_BOT_ON_STARTUP", settings.deploy.run_bot_on_startup);
        if let Ok(mode) = env::var("EXECUTION_MODE") {
            settings.live.execution_mode = if mode.eq_ignore_ascii_case("live") {
                ExecutionMode::Live
            } else {
                ExecutionMode::Paper
            };
        }
        settings.target.polymarket_gamma_url =
            env_string("POLYMARKET_GAMMA_URL", settings.target.polymarket_gamma_url);
        settings.target.polymarket_clob_url =
            env_string("POLYMARKET_CLOB_URL", settings.target.polymarket_clob_url);
        settings.target.polymarket_ws_url =
            env_string("POLYMARKET_WS_URL", settings.target.polymarket_ws_url);
        settings.target.polymarket_rtds_url =
            env_string("POLYMARKET_RTDS_URL", settings.target.polymarket_rtds_url);
        settings.target.chainlink_reference_url = env_non_empty("CHAINLINK_REFERENCE_URL");
        settings.target.chainlink_api_key = env_non_empty("CHAINLINK_API_KEY");
        settings.target.asset = env_string("TARGET_ASSET", settings.target.asset).to_uppercase();
        settings.target.asset_name = env_string("TARGET_ASSET_NAME", settings.target.asset_name);
        settings.target.horizon =
            env_string("TARGET_HORIZON", settings.target.horizon).to_ascii_lowercase();
        settings.target.resolution_source = env_string(
            "TARGET_RESOLUTION_SOURCE",
            settings.target.resolution_source,
        );
        settings.target.chainlink_symbol =
            env_string("TARGET_CHAINLINK_SYMBOL", settings.target.chainlink_symbol)
                .to_ascii_lowercase();
        settings.target.binance_symbol =
            env_string("TARGET_BINANCE_SYMBOL", settings.target.binance_symbol)
                .to_ascii_lowercase();
        settings.target.coinbase_product_id = env_string(
            "TARGET_COINBASE_PRODUCT_ID",
            settings.target.coinbase_product_id,
        );
        settings.target.discovery_limit =
            env_usize("DISCOVERY_LIMIT", settings.target.discovery_limit);
        settings.target.discovery_interval_seconds = env_f64(
            "DISCOVERY_INTERVAL_SECONDS",
            settings.target.discovery_interval_seconds,
        );
        settings.target.enable_polymarket_rtds_chainlink = env_bool(
            "ENABLE_POLYMARKET_RTDS_CHAINLINK",
            settings.target.enable_polymarket_rtds_chainlink,
        );
        settings.target.enable_polymarket_rtds_binance = env_bool(
            "ENABLE_POLYMARKET_RTDS_BINANCE",
            settings.target.enable_polymarket_rtds_binance,
        );
        settings.target.rtds_ping_interval_seconds = env_f64(
            "RTDS_PING_INTERVAL_SECONDS",
            settings.target.rtds_ping_interval_seconds,
        );
        settings.target.start_price_capture_grace_seconds = env_f64(
            "START_PRICE_CAPTURE_GRACE_SECONDS",
            settings.target.start_price_capture_grace_seconds,
        );
        settings.target.reference_divergence_pause_threshold = env_decimal(
            "REFERENCE_DIVERGENCE_PAUSE_THRESHOLD",
            settings.target.reference_divergence_pause_threshold,
        )?;
        settings.live.allow_live = env_bool("ALLOW_LIVE", settings.live.allow_live);
        settings.live.confirm_non_restricted_location = env_bool(
            "CONFIRM_NON_RESTRICTED_LOCATION",
            settings.live.confirm_non_restricted_location,
        );
        settings.live.allow_emergency_account_cancel = env_bool(
            "ALLOW_EMERGENCY_ACCOUNT_CANCEL",
            settings.live.allow_emergency_account_cancel,
        );
        settings.live.enable_heartbeat =
            env_bool("ENABLE_LIVE_HEARTBEAT", settings.live.enable_heartbeat);
        settings.live.heartbeat_interval_seconds = env_f64(
            "LIVE_HEARTBEAT_INTERVAL_SECONDS",
            settings.live.heartbeat_interval_seconds,
        );
        settings.live.heartbeat_failure_threshold = env_usize(
            "LIVE_HEARTBEAT_FAILURE_THRESHOLD",
            settings.live.heartbeat_failure_threshold,
        );
        settings.live.polymarket_private_key = env::var("POLYMARKET_PRIVATE_KEY").ok();
        settings.deploy.api_bearer_token = env::var("API_BEARER_TOKEN").ok();
        settings.deploy.require_api_auth =
            env_bool("REQUIRE_API_AUTH", settings.deploy.require_api_auth);
        settings.deploy.rust_upstream_api_base_url = env_non_empty("RUST_UPSTREAM_API_BASE_URL");
        settings.deploy.rust_upstream_ws_url = env_non_empty("RUST_UPSTREAM_WS_URL");
        settings.deploy.rust_proxy_runtime_api = env_bool(
            "RUST_PROXY_RUNTIME_API",
            settings.deploy.rust_upstream_api_base_url.is_some(),
        );
        settings.azure.storage_account_name = env::var("AZURE_STORAGE_ACCOUNT_NAME").ok();
        settings.azure.storage_container_name = env_string(
            "AZURE_STORAGE_CONTAINER_NAME",
            settings.azure.storage_container_name,
        );
        settings.azure.storage_table_name = env_string(
            "AZURE_STORAGE_TABLE_NAME",
            settings.azure.storage_table_name,
        );
        settings.azure.chart_table_name =
            env_string("AZURE_CHART_TABLE_NAME", settings.azure.chart_table_name);
        settings.azure.market_table_name =
            env_string("AZURE_MARKET_TABLE_NAME", settings.azure.market_table_name);
        settings.strategy.taker_min_edge =
            env_decimal("TAKER_MIN_EDGE", settings.strategy.taker_min_edge)?;
        settings.strategy.enable_taker_orders =
            env_bool("ENABLE_TAKER_ORDERS", settings.strategy.enable_taker_orders);
        settings.strategy.maker_margin =
            env_decimal("MAKER_MARGIN", settings.strategy.maker_margin)?;
        settings.strategy.maker_min_edge =
            env_decimal("MAKER_MIN_EDGE", settings.strategy.maker_min_edge)?;
        settings.strategy.adverse_selection_buffer = env_decimal(
            "ADVERSE_SELECTION_BUFFER",
            settings.strategy.adverse_selection_buffer,
        )?;
        settings.strategy.model_error_buffer =
            env_decimal("MODEL_ERROR_BUFFER", settings.strategy.model_error_buffer)?;
        settings.strategy.slippage_buffer =
            env_decimal("SLIPPAGE_BUFFER", settings.strategy.slippage_buffer)?;
        settings.strategy.ewma_lambda = env_f64("EWMA_LAMBDA", settings.strategy.ewma_lambda);
        settings.strategy.sigma_floor = env_f64("SIGMA_FLOOR", settings.strategy.sigma_floor);
        settings.strategy.sigma_cap = env_f64("SIGMA_CAP", settings.strategy.sigma_cap);
        settings.strategy.drift_mu = env_f64("DRIFT_MU", settings.strategy.drift_mu);
        settings.strategy.final_no_trade_seconds = env_i64(
            "FINAL_NO_TRADE_SECONDS",
            settings.strategy.final_no_trade_seconds,
        );
        settings.strategy.order_ttl_seconds =
            env_i64("ORDER_TTL_SECONDS", settings.strategy.order_ttl_seconds);
        settings.risk.base_order_size =
            env_decimal("BASE_ORDER_SIZE", settings.risk.base_order_size)?;
        settings.risk.max_order_size = env_decimal("MAX_ORDER_SIZE", settings.risk.max_order_size)?;
        settings.risk.max_position_per_market = env_decimal(
            "MAX_POSITION_PER_MARKET",
            settings.risk.max_position_per_market,
        )?;
        settings.risk.max_total_position =
            env_decimal("MAX_TOTAL_POSITION", settings.risk.max_total_position)?;
        settings.risk.max_daily_loss = env_decimal("MAX_DAILY_LOSS", settings.risk.max_daily_loss)?;
        settings.risk.max_open_orders = env_usize("MAX_OPEN_ORDERS", settings.risk.max_open_orders);
        settings.risk.max_reference_age_ms =
            env_i64("MAX_REFERENCE_AGE_MS", settings.risk.max_reference_age_ms);
        settings.risk.max_book_age_ms = env_i64("MAX_BOOK_AGE_MS", settings.risk.max_book_age_ms);
        settings.paper.maker_fill_policy =
            env_string("PAPER_MAKER_FILL_POLICY", settings.paper.maker_fill_policy);
        settings.paper.order_live_after_ms = env_i64(
            "PAPER_ORDER_LIVE_AFTER_MS",
            settings.paper.order_live_after_ms,
        );
        Ok(settings)
    }

    pub fn live_requested(&self) -> bool {
        self.live.execution_mode == ExecutionMode::Live
    }

    pub fn validate_live_gates(&self, exact_resolution_source: bool) -> Result<(), ConfigError> {
        if !self.live_requested() {
            return Ok(());
        }
        let mut reasons = Vec::new();
        if !self.live.allow_live {
            reasons.push("ALLOW_LIVE is false");
        }
        if !self.live.confirm_non_restricted_location {
            reasons.push("non-restricted location not confirmed");
        }
        if self.live.polymarket_private_key.is_none() {
            reasons.push("POLYMARKET_PRIVATE_KEY is not configured");
        }
        if self.live.require_exact_resolution_source_for_live && !exact_resolution_source {
            reasons.push("exact Chainlink resolution source unavailable");
        }
        if reasons.is_empty() {
            Ok(())
        } else {
            Err(ConfigError::LiveBlocked(reasons.join("; ")))
        }
    }

    pub fn status_config_payload(&self) -> Value {
        json!({
            "strategy": {
                "maker_margin": self.strategy.maker_margin.to_string(),
                "maker_min_edge": self.strategy.maker_min_edge.to_string(),
                "model_error_buffer": self.strategy.model_error_buffer.to_string(),
                "slippage_buffer": self.strategy.slippage_buffer.to_string(),
                "order_ttl_seconds": self.strategy.order_ttl_seconds,
                "final_no_trade_seconds": self.strategy.final_no_trade_seconds
            },
            "risk": {
                "base_order_size": self.risk.base_order_size.to_string(),
                "max_order_size": self.risk.max_order_size.to_string(),
                "max_position_per_market": self.risk.max_position_per_market.to_string(),
                "max_total_position": self.risk.max_total_position.to_string(),
                "max_daily_loss": self.risk.max_daily_loss.to_string(),
                "max_open_orders": self.risk.max_open_orders
            },
            "paper": {
                "paper_maker_fill_policy": self.paper.maker_fill_policy,
                "paper_order_live_after_ms": self.paper.order_live_after_ms
            },
            "read_only": {
                "execution_mode": match self.live.execution_mode {
                    ExecutionMode::Paper => "paper",
                    ExecutionMode::Live => "live"
                },
                "allow_live": self.live.allow_live,
                "live_requested": self.live_requested(),
                "require_exact_resolution_source_for_live": self.live.require_exact_resolution_source_for_live,
                "enable_taker_orders": self.strategy.enable_taker_orders,
                "allow_emergency_account_cancel": self.live.allow_emergency_account_cancel,
                "require_api_auth": self.deploy.require_api_auth,
                "rust_proxy_runtime_api": self.deploy.rust_proxy_runtime_api,
                "rust_upstream_api_configured": self.deploy.rust_upstream_api_base_url.is_some(),
                "rust_upstream_ws_configured": self.deploy.rust_upstream_ws_url.is_some(),
                "api_bearer_token_configured": self.deploy.api_bearer_token.is_some(),
                "polymarket_private_key_configured": self.live.polymarket_private_key.is_some(),
                "azure_storage_configured": self.azure.storage_account_name.is_some()
            }
        })
    }

    pub fn rtds_chainlink_source_name(&self) -> String {
        format!(
            "polymarket_rtds_chainlink_{}",
            normalize_source_symbol(&self.target.chainlink_symbol)
        )
    }

    pub fn rtds_binance_source_name(&self) -> String {
        format!(
            "polymarket_rtds_binance_{}",
            normalize_compact_symbol(&self.target.binance_symbol)
        )
    }

    pub fn binance_book_ticker_source_name(&self) -> String {
        format!(
            "binance_{}_book_ticker",
            normalize_compact_symbol(&self.target.binance_symbol)
        )
    }

    pub fn coinbase_ticker_source_name(&self) -> String {
        format!(
            "coinbase_{}_ticker",
            normalize_source_symbol(&self.target.coinbase_product_id)
        )
    }
}

fn env_non_empty(name: &str) -> Option<String> {
    env::var(name).ok().and_then(|value| {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed.to_owned())
        }
    })
}

fn env_string(name: &str, default: String) -> String {
    env_non_empty(name).unwrap_or(default)
}

fn env_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(default)
}

fn env_i64(name: &str, default: i64) -> i64 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(default)
}

fn env_f64(name: &str, default: f64) -> f64 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(default)
}

fn env_bool(name: &str, default: bool) -> bool {
    env::var(name)
        .map(|value| {
            matches!(
                value.to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(default)
}

fn normalize_source_symbol(value: &str) -> String {
    value.replace(['/', '-'], "_").to_ascii_lowercase()
}

fn normalize_compact_symbol(value: &str) -> String {
    value.replace(['/', '-'], "").to_ascii_lowercase()
}

fn env_decimal(name: &str, default: Decimal) -> Result<Decimal, ConfigError> {
    match env::var(name) {
        Ok(value) => Decimal::from_str_exact(&value).map_err(|_| ConfigError::InvalidDecimal {
            name: name.to_owned(),
            value,
        }),
        Err(_) => Ok(default),
    }
}
