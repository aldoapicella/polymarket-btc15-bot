use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fmt;

pub mod decimal_string {
    use rust_decimal::Decimal;
    use serde::de::{Error, Visitor};
    use serde::{Deserializer, Serializer};
    use std::fmt;

    pub fn serialize<S>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&value.to_string())
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Decimal, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_any(DecimalVisitor)
    }

    struct DecimalVisitor;

    impl<'de> Visitor<'de> for DecimalVisitor {
        type Value = Decimal;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("a decimal string or number")
        }

        fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
        where
            E: Error,
        {
            Decimal::from_str_exact(value).map_err(E::custom)
        }

        fn visit_string<E>(self, value: String) -> Result<Self::Value, E>
        where
            E: Error,
        {
            self.visit_str(&value)
        }

        fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E>
        where
            E: Error,
        {
            Ok(Decimal::from(value))
        }

        fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E>
        where
            E: Error,
        {
            Ok(Decimal::from(value))
        }

        fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
        where
            E: Error,
        {
            Decimal::from_f64_retain(value).ok_or_else(|| E::custom("invalid decimal float"))
        }
    }
}

pub mod decimal_string_opt {
    use rust_decimal::Decimal;
    use serde::de::Error;
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S>(value: &Option<Decimal>, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        match value {
            Some(decimal) => serializer.serialize_some(&decimal.to_string()),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Option<Decimal>, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = Option::<serde_json::Value>::deserialize(deserializer)?;
        match value {
            Some(serde_json::Value::String(text)) => Decimal::from_str_exact(&text)
                .map(Some)
                .map_err(D::Error::custom),
            Some(serde_json::Value::Number(number)) => Decimal::from_str_exact(&number.to_string())
                .map(Some)
                .map_err(D::Error::custom),
            Some(serde_json::Value::Null) | None => Ok(None),
            Some(other) => Err(D::Error::custom(format!("invalid decimal value: {other}"))),
        }
    }
}

macro_rules! string_id {
    ($name:ident) => {
        #[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(pub String);

        impl $name {
            pub fn new(value: impl Into<String>) -> Self {
                Self(value.into())
            }
        }

        impl From<&str> for $name {
            fn from(value: &str) -> Self {
                Self(value.to_owned())
            }
        }

        impl From<String> for $name {
            fn from(value: String) -> Self {
                Self(value)
            }
        }

        impl AsRef<str> for $name {
            fn as_ref(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

string_id!(MarketId);
string_id!(ConditionId);
string_id!(TokenId);
string_id!(OrderId);

pub type Probability = Decimal;
pub type PriceTicks = Decimal;
pub type ShareSize = Decimal;
pub type UsdPrice = Decimal;

fn default_asset() -> String {
    "BTC".to_owned()
}

fn default_horizon() -> String {
    "15m".to_owned()
}

fn default_resolution_source() -> String {
    "chainlink_reference".to_owned()
}

fn default_tick_size() -> Decimal {
    Decimal::new(1, 2)
}

fn default_minimum_order_size() -> Decimal {
    Decimal::from(5)
}

fn utc_now() -> DateTime<Utc> {
    Utc::now()
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Up,
    Down,
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderKind {
    PostOnlyGtc,
    PostOnlyGtd,
    Fak,
    Fok,
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionAction {
    Place,
    CancelAll,
    Hold,
}

#[derive(Clone, Debug, Default, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MarketStatus {
    Tradeable,
    #[default]
    ObserveOnly,
    Closed,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct BookLevel {
    #[serde(with = "decimal_string")]
    pub price: Decimal,
    #[serde(with = "decimal_string")]
    pub size: Decimal,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct MarketSpec {
    #[serde(default = "default_asset")]
    pub asset: String,
    #[serde(default = "default_horizon")]
    pub horizon: String,
    #[serde(default)]
    pub event_id: Option<String>,
    #[serde(default)]
    pub event_slug: Option<String>,
    pub market_id: MarketId,
    #[serde(default)]
    pub market_slug: Option<String>,
    pub condition_id: ConditionId,
    pub question: String,
    #[serde(default)]
    pub description: Option<String>,
    pub up_token_id: TokenId,
    pub down_token_id: TokenId,
    pub start_ts: DateTime<Utc>,
    pub end_ts: DateTime<Utc>,
    #[serde(default, with = "decimal_string_opt")]
    pub start_price: Option<Decimal>,
    #[serde(default = "default_resolution_source")]
    pub resolution_source: String,
    #[serde(default = "default_tick_size", with = "decimal_string")]
    pub tick_size: Decimal,
    #[serde(default = "default_minimum_order_size", with = "decimal_string")]
    pub minimum_order_size: Decimal,
    #[serde(default)]
    pub neg_risk: bool,
    #[serde(default = "default_true")]
    pub fees_enabled: bool,
    #[serde(default = "default_true")]
    pub accepting_orders: bool,
    #[serde(default)]
    pub status: MarketStatus,
    #[serde(default)]
    pub raw: BTreeMap<String, Value>,
}

impl MarketSpec {
    pub fn is_tradeable(&self) -> bool {
        self.status == MarketStatus::Tradeable && self.start_price.is_some()
    }

    pub fn with_start_price(mut self, price: Decimal) -> Self {
        self.start_price = Some(price);
        self.status = if self.accepting_orders {
            MarketStatus::Tradeable
        } else {
            MarketStatus::ObserveOnly
        };
        self
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct BookState {
    pub token_id: TokenId,
    #[serde(default)]
    pub bids: Vec<BookLevel>,
    #[serde(default)]
    pub asks: Vec<BookLevel>,
    #[serde(default, with = "decimal_string_opt")]
    pub last_trade_price: Option<Decimal>,
    #[serde(default)]
    pub exchange_ts: Option<DateTime<Utc>>,
    #[serde(default = "utc_now")]
    pub local_ts: DateTime<Utc>,
    #[serde(default)]
    pub book_hash: Option<String>,
}

impl BookState {
    pub fn best_bid(&self) -> Option<&BookLevel> {
        self.bids
            .iter()
            .max_by(|left, right| left.price.cmp(&right.price))
    }

    pub fn best_ask(&self) -> Option<&BookLevel> {
        self.asks
            .iter()
            .min_by(|left, right| left.price.cmp(&right.price))
    }

    pub fn age_ms(&self, now: DateTime<Utc>) -> f64 {
        let age = now.signed_duration_since(self.local_ts);
        age.num_microseconds()
            .map_or(0.0, |micros| (micros.max(0) as f64) / 1000.0)
    }

    pub fn is_stale(&self, max_age_ms: i64, now: DateTime<Utc>) -> bool {
        self.age_ms(now) > max_age_ms as f64
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ReferencePrice {
    pub source: String,
    #[serde(with = "decimal_string")]
    pub price: Decimal,
    pub source_ts: DateTime<Utc>,
    #[serde(default = "utc_now")]
    pub local_ts: DateTime<Utc>,
    #[serde(default)]
    pub latency_ms: f64,
    #[serde(default)]
    pub stale: bool,
    #[serde(default)]
    pub exact_resolution_source: bool,
    #[serde(default)]
    pub quality_flags: Vec<String>,
}

impl ReferencePrice {
    pub fn age_ms(&self, now: DateTime<Utc>) -> f64 {
        let age = now.signed_duration_since(self.local_ts);
        age.num_microseconds()
            .map_or(0.0, |micros| (micros.max(0) as f64) / 1000.0)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FairValue {
    pub market_id: MarketId,
    #[serde(with = "decimal_string")]
    pub q_up: Decimal,
    #[serde(with = "decimal_string")]
    pub q_down: Decimal,
    pub sigma: f64,
    pub drift_mu: f64,
    #[serde(with = "decimal_string")]
    pub model_error: Decimal,
    #[serde(default = "utc_now")]
    pub computed_ts: DateTime<Utc>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TradeDecision {
    pub action: DecisionAction,
    pub market_id: MarketId,
    #[serde(default)]
    pub condition_id: Option<ConditionId>,
    #[serde(default)]
    pub token_id: Option<TokenId>,
    #[serde(default)]
    pub outcome: Option<Outcome>,
    #[serde(default)]
    pub side: Option<Side>,
    #[serde(default, with = "decimal_string_opt")]
    pub price: Option<Decimal>,
    #[serde(default, with = "decimal_string_opt")]
    pub size: Option<Decimal>,
    #[serde(default, with = "decimal_string_opt")]
    pub quote_amount: Option<Decimal>,
    #[serde(default)]
    pub order_kind: Option<OrderKind>,
    pub reason: String,
    #[serde(default)]
    pub ttl_ms: Option<i64>,
    #[serde(default, with = "decimal_string_opt")]
    pub expected_edge: Option<Decimal>,
    #[serde(default)]
    pub post_only: bool,
    #[serde(default, with = "decimal_string_opt")]
    pub tick_size: Option<Decimal>,
    #[serde(default)]
    pub neg_risk: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ExecutionReport {
    #[serde(default)]
    pub order_id: Option<OrderId>,
    pub market_id: MarketId,
    #[serde(default)]
    pub token_id: Option<TokenId>,
    pub status: String,
    #[serde(default, with = "decimal_string")]
    pub filled_size: Decimal,
    #[serde(default, with = "decimal_string_opt")]
    pub avg_price: Option<Decimal>,
    #[serde(default, with = "decimal_string")]
    pub fee: Decimal,
    #[serde(default = "utc_now")]
    pub local_ts: DateTime<Utc>,
    #[serde(default)]
    pub raw: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RiskAssessment {
    pub allowed: bool,
    #[serde(default)]
    pub reasons: Vec<String>,
}

impl RiskAssessment {
    pub fn allow() -> Self {
        Self {
            allowed: true,
            reasons: Vec::new(),
        }
    }

    pub fn deny(reasons: Vec<String>) -> Self {
        Self {
            allowed: false,
            reasons: reasons
                .into_iter()
                .filter(|reason| !reason.is_empty())
                .collect(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RuntimeEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub ts: DateTime<Utc>,
    #[serde(default)]
    pub data: Value,
}

fn default_true() -> bool {
    true
}
