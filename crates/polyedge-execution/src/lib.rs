use async_trait::async_trait;
use chrono::{DateTime, Utc};
use polyedge_config::RuntimeSettings;
use polyedge_domain::{
    decimal_string, DecisionAction, ExecutionReport, MarketId, OrderId, OrderKind, TokenId,
    TradeDecision,
};
use polyedge_engine::crypto_taker_fee_per_share;
use rust_decimal::Decimal;
use serde_json::json;
use std::collections::BTreeMap;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ExecutionError {
    #[error("live trading blocked: {0}")]
    LiveTradingBlocked(String),
}

#[async_trait]
pub trait ExecutionClient {
    async fn submit(&mut self, decision: &TradeDecision)
        -> Result<ExecutionReport, ExecutionError>;
    async fn cancel_all(
        &mut self,
        market_id: Option<&MarketId>,
    ) -> Result<Vec<ExecutionReport>, ExecutionError>;
}

#[derive(Clone, Debug)]
pub struct PaperRestingOrder {
    pub order_id: OrderId,
    pub decision: TradeDecision,
    pub report: ExecutionReport,
}

#[derive(Clone, Debug, Default)]
pub struct PaperExecutionClient {
    next_id: u64,
    pub resting_orders: BTreeMap<OrderId, PaperRestingOrder>,
}

impl PaperExecutionClient {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn open_orders(&self) -> BTreeMap<OrderId, TradeDecision> {
        self.resting_orders
            .iter()
            .map(|(order_id, resting)| (order_id.clone(), resting.decision.clone()))
            .collect()
    }

    pub fn resting_for_token(&self, token_id: &TokenId) -> Vec<PaperRestingOrder> {
        self.resting_orders
            .values()
            .filter(|resting| resting.decision.token_id.as_ref() == Some(token_id))
            .cloned()
            .collect()
    }

    pub fn fill_maker_order(
        &mut self,
        order_id: &OrderId,
        avg_price: Decimal,
        local_ts: DateTime<Utc>,
    ) -> Option<ExecutionReport> {
        let resting = self.resting_orders.remove(order_id)?;
        let raw = decision_raw(&resting.decision);
        Some(ExecutionReport {
            order_id: Some(order_id.clone()),
            market_id: resting.decision.market_id,
            token_id: resting.decision.token_id,
            status: "paper_filled_maker".to_owned(),
            filled_size: resting.decision.size.unwrap_or(Decimal::ZERO),
            avg_price: Some(avg_price),
            fee: Decimal::ZERO,
            local_ts,
            raw,
        })
    }

    pub fn clear_market(&mut self, market_id: &MarketId) {
        self.resting_orders
            .retain(|_, resting| &resting.decision.market_id != market_id);
    }

    fn next_order_id(&mut self) -> OrderId {
        self.next_id += 1;
        OrderId::new(format!("paper-{}", self.next_id))
    }
}

#[async_trait]
impl ExecutionClient for PaperExecutionClient {
    async fn submit(
        &mut self,
        decision: &TradeDecision,
    ) -> Result<ExecutionReport, ExecutionError> {
        if decision.action == DecisionAction::CancelAll {
            let reports = self.cancel_all(Some(&decision.market_id)).await?;
            return Ok(reports.last().cloned().unwrap_or_else(|| ExecutionReport {
                order_id: None,
                market_id: decision.market_id.clone(),
                token_id: None,
                status: "paper_cancel_all_noop".to_owned(),
                filled_size: Decimal::ZERO,
                avg_price: None,
                fee: Decimal::ZERO,
                local_ts: Utc::now(),
                raw: BTreeMap::new(),
            }));
        }
        if decision.action != DecisionAction::Place {
            return Ok(ExecutionReport {
                order_id: None,
                market_id: decision.market_id.clone(),
                token_id: decision.token_id.clone(),
                status: format!("paper_{:?}", decision.action).to_ascii_lowercase(),
                filled_size: Decimal::ZERO,
                avg_price: None,
                fee: Decimal::ZERO,
                local_ts: Utc::now(),
                raw: BTreeMap::new(),
            });
        }
        let order_id = self.next_order_id();
        let filled = if matches!(
            decision.order_kind.as_ref(),
            Some(OrderKind::Fak | OrderKind::Fok)
        ) {
            decision.size
        } else {
            None
        };
        let fee = match (filled, decision.price) {
            (Some(size), Some(price)) => {
                crypto_taker_fee_per_share(price).unwrap_or(Decimal::ZERO) * size
            }
            _ => Decimal::ZERO,
        };
        let report = ExecutionReport {
            order_id: Some(order_id.clone()),
            market_id: decision.market_id.clone(),
            token_id: decision.token_id.clone(),
            status: if filled.is_some() {
                "paper_filled".to_owned()
            } else {
                "paper_resting".to_owned()
            },
            filled_size: filled.unwrap_or(Decimal::ZERO),
            avg_price: if filled.is_some() {
                decision.price
            } else {
                None
            },
            fee,
            local_ts: Utc::now(),
            raw: decision_raw(decision),
        };
        if filled.is_none() {
            self.resting_orders.insert(
                order_id.clone(),
                PaperRestingOrder {
                    order_id,
                    decision: decision.clone(),
                    report: report.clone(),
                },
            );
        }
        Ok(report)
    }

    async fn cancel_all(
        &mut self,
        market_id: Option<&MarketId>,
    ) -> Result<Vec<ExecutionReport>, ExecutionError> {
        let mut cancelled = Vec::new();
        for (order_id, resting) in self.resting_orders.clone() {
            if market_id.is_some_and(|target| target != &resting.decision.market_id) {
                continue;
            }
            self.resting_orders.remove(&order_id);
            cancelled.push(ExecutionReport {
                order_id: Some(order_id),
                market_id: resting.decision.market_id.clone(),
                token_id: resting.decision.token_id.clone(),
                status: "paper_cancelled".to_owned(),
                filled_size: Decimal::ZERO,
                avg_price: None,
                fee: Decimal::ZERO,
                local_ts: Utc::now(),
                raw: decision_raw(&resting.decision),
            });
        }
        Ok(cancelled)
    }
}

pub struct LiveClobExecutionClient {
    settings: RuntimeSettings,
}

impl LiveClobExecutionClient {
    pub fn try_new(settings: RuntimeSettings) -> Result<Self, ExecutionError> {
        #[cfg(not(feature = "live"))]
        {
            let _ = settings;
            Err(ExecutionError::LiveTradingBlocked(
                "polyedge-execution was compiled without the live feature".to_owned(),
            ))
        }
        #[cfg(feature = "live")]
        {
            settings
                .validate_live_gates(false)
                .map_err(|error| ExecutionError::LiveTradingBlocked(error.to_string()))?;
            Ok(Self { settings })
        }
    }

    pub fn heartbeat_status(&self) -> serde_json::Value {
        json!({
            "enabled": self.settings.live.enable_heartbeat,
            "interval_seconds": self.settings.live.heartbeat_interval_seconds,
            "failure_threshold": self.settings.live.heartbeat_failure_threshold,
            "status": "stubbed"
        })
    }
}

#[async_trait]
impl ExecutionClient for LiveClobExecutionClient {
    async fn submit(
        &mut self,
        _decision: &TradeDecision,
    ) -> Result<ExecutionReport, ExecutionError> {
        Err(ExecutionError::LiveTradingBlocked(
            "live order placement is intentionally not implemented in the Rust shadow backend"
                .to_owned(),
        ))
    }

    async fn cancel_all(
        &mut self,
        _market_id: Option<&MarketId>,
    ) -> Result<Vec<ExecutionReport>, ExecutionError> {
        Err(ExecutionError::LiveTradingBlocked(
            "live cancellation is intentionally not implemented in the Rust shadow backend"
                .to_owned(),
        ))
    }
}

fn decision_raw(decision: &TradeDecision) -> BTreeMap<String, serde_json::Value> {
    let mut raw = BTreeMap::new();
    raw.insert(
        "decision".to_owned(),
        serde_json::to_value(decision).unwrap_or_else(|_| json!({"serialization_error": true})),
    );
    raw
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct PaperFillStatus {
    pub paper_fill_policy: String,
    pub paper_order_live_after_ms: i64,
    pub paper_open_resting_orders: usize,
    #[serde(with = "decimal_string")]
    pub total_resting_notional: Decimal,
}
