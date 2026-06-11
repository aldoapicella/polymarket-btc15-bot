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

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionMode {
    Paper,
    Live,
}

impl Default for ExecutionMode {
    fn default() -> Self {
        Self::Paper
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DeployConfig {
    pub app_name: String,
    pub run_bot_on_startup: bool,
    pub require_api_auth: bool,
    #[serde(skip_serializing)]
    pub api_bearer_token: Option<String>,
}

impl Default for DeployConfig {
    fn default() -> Self {
        Self {
            app_name: "polyedge".to_owned(),
            run_bot_on_startup: false,
            require_api_auth: false,
            api_bearer_token: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TargetConfig {
    pub asset: String,
    pub asset_name: String,
    pub horizon: String,
    pub resolution_source: String,
    pub chainlink_symbol: String,
    pub binance_symbol: String,
    pub coinbase_product_id: String,
    pub discovery_limit: usize,
    pub discovery_interval_seconds: f64,
}

impl Default for TargetConfig {
    fn default() -> Self {
        Self {
            asset: "BTC".to_owned(),
            asset_name: "Bitcoin".to_owned(),
            horizon: "15m".to_owned(),
            resolution_source: "chainlink_reference".to_owned(),
            chainlink_symbol: "btc/usd".to_owned(),
            binance_symbol: "btcusdt".to_owned(),
            coinbase_product_id: "BTC-USD".to_owned(),
            discovery_limit: 250,
            discovery_interval_seconds: 20.0,
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
        if let Ok(mode) = env::var("EXECUTION_MODE") {
            settings.live.execution_mode = if mode.eq_ignore_ascii_case("live") {
                ExecutionMode::Live
            } else {
                ExecutionMode::Paper
            };
        }
        settings.live.allow_live = env_bool("ALLOW_LIVE", settings.live.allow_live);
        settings.live.confirm_non_restricted_location = env_bool(
            "CONFIRM_NON_RESTRICTED_LOCATION",
            settings.live.confirm_non_restricted_location,
        );
        settings.live.polymarket_private_key = env::var("POLYMARKET_PRIVATE_KEY").ok();
        settings.deploy.api_bearer_token = env::var("API_BEARER_TOKEN").ok();
        settings.deploy.require_api_auth =
            env_bool("REQUIRE_API_AUTH", settings.deploy.require_api_auth);
        settings.azure.storage_account_name = env::var("AZURE_STORAGE_ACCOUNT_NAME").ok();
        settings.strategy.maker_margin =
            env_decimal("MAKER_MARGIN", settings.strategy.maker_margin)?;
        settings.strategy.maker_min_edge =
            env_decimal("MAKER_MIN_EDGE", settings.strategy.maker_min_edge)?;
        settings.risk.base_order_size =
            env_decimal("BASE_ORDER_SIZE", settings.risk.base_order_size)?;
        settings.risk.max_order_size = env_decimal("MAX_ORDER_SIZE", settings.risk.max_order_size)?;
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
                "api_bearer_token_configured": self.deploy.api_bearer_token.is_some(),
                "polymarket_private_key_configured": self.live.polymarket_private_key.is_some(),
                "azure_storage_configured": self.azure.storage_account_name.is_some()
            }
        })
    }
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

fn env_decimal(name: &str, default: Decimal) -> Result<Decimal, ConfigError> {
    match env::var(name) {
        Ok(value) => Decimal::from_str_exact(&value).map_err(|_| ConfigError::InvalidDecimal {
            name: name.to_owned(),
            value,
        }),
        Err(_) => Ok(default),
    }
}
