use chrono::{DateTime, Duration, Utc};
use polyedge_config::RuntimeSettings;
use polyedge_domain::{
    BookState, ConditionId, ExecutionReport, FairValue, MarketId, MarketSpec, OrderId, OrderKind,
    Outcome, ReferencePrice, RiskAssessment, Side, TokenId, TradeDecision,
};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use thiserror::Error;

pub const SECONDS_PER_YEAR: f64 = 365.0 * 24.0 * 60.0 * 60.0;

#[derive(Debug, Error)]
pub enum EngineError {
    #[error("tick size must be positive")]
    InvalidTickSize,
    #[error("price must be between 0 and 1")]
    InvalidProbabilityPrice,
}

pub fn clamp(value: f64, lower: f64, upper: f64) -> f64 {
    value.max(lower).min(upper)
}

pub fn normal_cdf(value: f64) -> f64 {
    0.5 * (1.0 + libm::erf(value / 2.0_f64.sqrt()))
}

pub fn crypto_taker_fee_per_share(price: Decimal) -> Result<Decimal, EngineError> {
    if price < Decimal::ZERO || price > Decimal::ONE {
        return Err(EngineError::InvalidProbabilityPrice);
    }
    Ok(Decimal::new(7, 2) * price * (Decimal::ONE - price))
}

pub fn floor_to_tick(price: Decimal, tick_size: Decimal) -> Result<Decimal, EngineError> {
    if tick_size <= Decimal::ZERO {
        return Err(EngineError::InvalidTickSize);
    }
    Ok((price / tick_size).floor() * tick_size)
}

#[derive(Clone, Debug)]
pub struct EwmaVolatilityEstimator {
    lambda: f64,
    sigma_floor: f64,
    sigma_cap: f64,
    last_price: Option<f64>,
    last_ts: Option<DateTime<Utc>>,
    variance_per_second: f64,
}

impl EwmaVolatilityEstimator {
    pub fn new(lambda: f64, sigma_floor: f64, sigma_cap: f64) -> Self {
        let daily_var = (sigma_floor / 365.0_f64.sqrt()).powi(2);
        Self {
            lambda,
            sigma_floor,
            sigma_cap,
            last_price: None,
            last_ts: None,
            variance_per_second: daily_var / (24.0 * 60.0 * 60.0),
        }
    }

    pub fn update(&mut self, reference: &ReferencePrice) -> f64 {
        let Some(price) = reference.price.to_f64() else {
            return self.sigma();
        };
        if price <= 0.0 {
            return self.sigma();
        }
        if let (Some(last_price), Some(last_ts)) = (self.last_price, self.last_ts) {
            let dt = reference
                .source_ts
                .signed_duration_since(last_ts)
                .num_microseconds()
                .map_or(0.001, |micros| (micros as f64 / 1_000_000.0).max(0.001));
            let log_return = (price / last_price).ln();
            let realized_var_per_second = (log_return * log_return) / dt;
            self.variance_per_second = self.lambda * self.variance_per_second
                + (1.0 - self.lambda) * realized_var_per_second;
        }
        self.last_price = Some(price);
        self.last_ts = Some(reference.source_ts);
        self.sigma()
    }

    pub fn sigma(&self) -> f64 {
        clamp(
            (self.variance_per_second.max(0.0) * SECONDS_PER_YEAR).sqrt(),
            self.sigma_floor,
            self.sigma_cap,
        )
    }
}

#[derive(Clone, Debug)]
pub struct LogReturnFairValueModel {
    settings: RuntimeSettings,
    volatility: EwmaVolatilityEstimator,
}

impl LogReturnFairValueModel {
    pub fn new(settings: RuntimeSettings) -> Self {
        let volatility = EwmaVolatilityEstimator::new(
            settings.strategy.ewma_lambda,
            settings.strategy.sigma_floor,
            settings.strategy.sigma_cap,
        );
        Self {
            settings,
            volatility,
        }
    }

    pub fn update_volatility(&mut self, reference: &ReferencePrice) -> f64 {
        self.volatility.update(reference)
    }

    pub fn compute(
        &self,
        market: &MarketSpec,
        reference: &ReferencePrice,
        now: DateTime<Utc>,
        sigma: Option<f64>,
        drift_mu: Option<f64>,
    ) -> Option<FairValue> {
        let start_price = market.start_price?;
        if start_price <= Decimal::ZERO || reference.price <= Decimal::ZERO {
            return None;
        }
        let seconds_remaining = market
            .end_ts
            .signed_duration_since(now)
            .num_microseconds()
            .map_or(0.0, |micros| micros as f64 / 1_000_000.0);
        if seconds_remaining <= 0.0 {
            return None;
        }

        let sigma_value = clamp(
            sigma.unwrap_or_else(|| self.volatility.sigma()),
            self.settings.strategy.sigma_floor,
            self.settings.strategy.sigma_cap,
        );
        let tau = seconds_remaining / SECONDS_PER_YEAR;
        let drift = drift_mu.unwrap_or(self.settings.strategy.drift_mu);
        let denominator = sigma_value * tau.max(1e-12).sqrt();
        let reference_float = reference.price.to_f64()?;
        let start_float = start_price.to_f64()?;
        let numerator = (reference_float / start_float).ln() + drift * tau;
        let z_score = numerator / denominator;
        let q_up_float = clamp(normal_cdf(z_score), 0.001, 0.999);
        let q_up = decimal_from_rounded_f64(q_up_float, 6)?;
        Some(FairValue {
            market_id: market.market_id.clone(),
            q_up,
            q_down: Decimal::ONE - q_up,
            sigma: sigma_value,
            drift_mu: drift,
            model_error: self.settings.strategy.model_error_buffer,
            computed_ts: now,
        })
    }
}

#[derive(Clone, Debug)]
pub struct MakerFirstStrategy {
    settings: RuntimeSettings,
}

impl MakerFirstStrategy {
    pub fn new(settings: RuntimeSettings) -> Self {
        Self { settings }
    }

    pub fn evaluate(
        &self,
        market: &MarketSpec,
        fair_value: &FairValue,
        books: &BTreeMap<TokenId, BookState>,
    ) -> Vec<TradeDecision> {
        let mut decisions = Vec::new();
        decisions.extend(self.evaluate_outcome(
            market,
            Outcome::Up,
            &market.up_token_id,
            fair_value.q_up,
            books.get(&market.up_token_id),
            fair_value.model_error,
        ));
        decisions.extend(self.evaluate_outcome(
            market,
            Outcome::Down,
            &market.down_token_id,
            fair_value.q_down,
            books.get(&market.down_token_id),
            fair_value.model_error,
        ));
        if decisions.is_empty() {
            vec![TradeDecision {
                action: polyedge_domain::DecisionAction::Hold,
                market_id: market.market_id.clone(),
                condition_id: Some(market.condition_id.clone()),
                token_id: None,
                outcome: None,
                side: None,
                price: None,
                size: None,
                quote_amount: None,
                order_kind: None,
                reason: "no maker or taker edge after fees and buffers".to_owned(),
                ttl_ms: None,
                expected_edge: None,
                post_only: false,
                tick_size: None,
                neg_risk: false,
            }]
        } else {
            decisions
        }
    }

    fn evaluate_outcome(
        &self,
        market: &MarketSpec,
        outcome: Outcome,
        token_id: &TokenId,
        fair_probability: Decimal,
        book: Option<&BookState>,
        model_error: Decimal,
    ) -> Vec<TradeDecision> {
        let Some(book) = book else {
            return Vec::new();
        };
        let (Some(best_bid), Some(best_ask)) = (book.best_bid(), book.best_ask()) else {
            return Vec::new();
        };
        let target_price = match floor_to_tick(
            fair_probability - self.settings.strategy.maker_margin,
            market.tick_size,
        ) {
            Ok(value) => value,
            Err(_) => return Vec::new(),
        };
        let max_price_for_edge = match floor_to_tick(
            fair_probability
                - self.settings.strategy.adverse_selection_buffer
                - model_error
                - self.settings.strategy.maker_min_edge,
            market.tick_size,
        ) {
            Ok(value) => value,
            Err(_) => return Vec::new(),
        };
        let competitive_price =
            match floor_to_tick(best_bid.price + market.tick_size, market.tick_size) {
                Ok(value) => value,
                Err(_) => return Vec::new(),
            };
        let mut maker_price = target_price.min(max_price_for_edge);
        if competitive_price <= maker_price {
            maker_price = competitive_price;
        }
        let maker_edge = fair_probability
            - maker_price
            - self.settings.strategy.adverse_selection_buffer
            - model_error;
        let order_size = self
            .settings
            .risk
            .base_order_size
            .min(self.settings.risk.max_order_size);
        let mut decisions = Vec::new();
        if maker_price > best_bid.price
            && maker_price < best_ask.price
            && maker_price > Decimal::ZERO
            && maker_price < Decimal::ONE
            && maker_edge >= self.settings.strategy.maker_min_edge
        {
            decisions.push(TradeDecision {
                action: polyedge_domain::DecisionAction::Place,
                market_id: market.market_id.clone(),
                condition_id: Some(market.condition_id.clone()),
                token_id: Some(token_id.clone()),
                outcome: Some(outcome.clone()),
                side: Some(Side::Buy),
                price: Some(maker_price),
                size: Some(order_size),
                quote_amount: None,
                order_kind: Some(OrderKind::PostOnlyGtc),
                reason: "maker edge exceeds threshold".to_owned(),
                ttl_ms: Some(self.settings.strategy.order_ttl_seconds * 1000),
                expected_edge: Some(maker_edge),
                post_only: true,
                tick_size: Some(market.tick_size),
                neg_risk: market.neg_risk,
            });
        }

        if self.settings.strategy.enable_taker_orders {
            let taker_fee = if market.fees_enabled {
                crypto_taker_fee_per_share(best_ask.price).unwrap_or(Decimal::ZERO)
            } else {
                Decimal::ZERO
            };
            let taker_edge = fair_probability
                - best_ask.price
                - taker_fee
                - self.settings.strategy.slippage_buffer
                - model_error;
            if taker_edge >= self.settings.strategy.taker_min_edge {
                decisions.push(TradeDecision {
                    action: polyedge_domain::DecisionAction::Place,
                    market_id: market.market_id.clone(),
                    condition_id: Some(market.condition_id.clone()),
                    token_id: Some(token_id.clone()),
                    outcome: Some(outcome),
                    side: Some(Side::Buy),
                    price: Some(best_ask.price),
                    size: Some(order_size),
                    quote_amount: Some(best_ask.price * order_size),
                    order_kind: Some(OrderKind::Fak),
                    reason: "taker edge exceeds high threshold".to_owned(),
                    ttl_ms: Some(1000),
                    expected_edge: Some(taker_edge),
                    post_only: false,
                    tick_size: Some(market.tick_size),
                    neg_risk: market.neg_risk,
                });
            }
        }
        decisions
    }
}

#[derive(Clone, Debug)]
pub struct RiskManager {
    settings: RuntimeSettings,
    positions_by_market: BTreeMap<MarketId, Decimal>,
    total_position: Decimal,
    daily_pnl: Decimal,
    pub open_order_count: usize,
}

impl RiskManager {
    pub fn new(settings: RuntimeSettings) -> Self {
        Self {
            settings,
            positions_by_market: BTreeMap::new(),
            total_position: Decimal::ZERO,
            daily_pnl: Decimal::ZERO,
            open_order_count: 0,
        }
    }

    pub fn assess_market(
        &self,
        market: &MarketSpec,
        reference: &ReferencePrice,
        books: &BTreeMap<TokenId, BookState>,
        now: DateTime<Utc>,
        kill_switch_enabled: bool,
    ) -> RiskAssessment {
        let mut reasons = Vec::new();
        if kill_switch_enabled {
            reasons.push("kill switch file exists".to_owned());
        }
        if self.settings.live_requested() {
            if !self.settings.live.allow_live {
                reasons.push("ALLOW_LIVE is false".to_owned());
            }
            if !self.settings.live.confirm_non_restricted_location {
                reasons.push("non-restricted location not confirmed".to_owned());
            }
            if self.settings.live.polymarket_private_key.is_none() {
                reasons.push("POLYMARKET_PRIVATE_KEY is not configured".to_owned());
            }
            if self.settings.live.require_exact_resolution_source_for_live
                && !reference.exact_resolution_source
            {
                reasons.push("exact Chainlink resolution source unavailable".to_owned());
            }
        }
        if !market.is_tradeable() {
            reasons.push("market is not tradeable".to_owned());
        }
        if reference.stale || reference.age_ms(now) > self.settings.risk.max_reference_age_ms as f64
        {
            reasons.push("reference price is stale".to_owned());
            reasons.extend(reference.quality_flags.iter().cloned());
        }
        for token_id in [&market.up_token_id, &market.down_token_id] {
            match books.get(token_id) {
                Some(book) if book.is_stale(self.settings.risk.max_book_age_ms, now) => {
                    reasons.push(format!("stale book for token {token_id}"));
                }
                Some(_) => {}
                None => reasons.push(format!("missing book for token {token_id}")),
            }
        }
        let seconds_to_close = market
            .end_ts
            .signed_duration_since(now)
            .num_microseconds()
            .map_or(0.0, |micros| micros as f64 / 1_000_000.0);
        if seconds_to_close <= self.settings.strategy.final_no_trade_seconds as f64 {
            reasons.push("inside final no-trade window".to_owned());
        }
        if self.daily_pnl <= -self.settings.risk.max_daily_loss {
            reasons.push("max daily loss reached".to_owned());
        }
        if self.total_position >= self.settings.risk.max_total_position {
            reasons.push("max total position reached".to_owned());
        }
        if self.open_order_count >= self.settings.risk.max_open_orders {
            reasons.push("max open orders reached".to_owned());
        }
        if reasons.is_empty() {
            RiskAssessment::allow()
        } else {
            RiskAssessment::deny(reasons)
        }
    }

    pub fn filter_decisions(
        &self,
        decisions: &[TradeDecision],
        market: &MarketSpec,
        assessment: &RiskAssessment,
    ) -> Vec<TradeDecision> {
        if !assessment.allowed {
            return vec![TradeDecision {
                action: polyedge_domain::DecisionAction::CancelAll,
                market_id: market.market_id.clone(),
                condition_id: Some(market.condition_id.clone()),
                token_id: None,
                outcome: None,
                side: None,
                price: None,
                size: None,
                quote_amount: None,
                order_kind: None,
                reason: assessment.reasons.join("; "),
                ttl_ms: None,
                expected_edge: None,
                post_only: false,
                tick_size: None,
                neg_risk: false,
            }];
        }
        let mut filtered = Vec::new();
        for decision in decisions {
            if decision.action != polyedge_domain::DecisionAction::Place {
                filtered.push(decision.clone());
                continue;
            }
            let Some(size) = decision.size else {
                continue;
            };
            let mut candidate = decision.clone();
            if size > self.settings.risk.max_order_size {
                candidate.size = Some(self.settings.risk.max_order_size);
            }
            let projected_market = self
                .positions_by_market
                .get(&market.market_id)
                .copied()
                .unwrap_or(Decimal::ZERO)
                + candidate.size.unwrap_or(Decimal::ZERO);
            let projected_total = self.total_position + candidate.size.unwrap_or(Decimal::ZERO);
            if projected_market > self.settings.risk.max_position_per_market {
                continue;
            }
            if projected_total > self.settings.risk.max_total_position {
                continue;
            }
            filtered.push(candidate);
        }
        if filtered.is_empty() {
            vec![TradeDecision {
                action: polyedge_domain::DecisionAction::Hold,
                market_id: market.market_id.clone(),
                condition_id: Some(market.condition_id.clone()),
                token_id: None,
                outcome: None,
                side: None,
                price: None,
                size: None,
                quote_amount: None,
                order_kind: None,
                reason: "all decisions rejected by risk limits".to_owned(),
                ttl_ms: None,
                expected_edge: None,
                post_only: false,
                tick_size: None,
                neg_risk: false,
            }]
        } else {
            filtered
        }
    }

    pub fn on_execution_report(&mut self, report: &ExecutionReport) {
        if report.filled_size <= Decimal::ZERO {
            return;
        }
        *self
            .positions_by_market
            .entry(report.market_id.clone())
            .or_insert(Decimal::ZERO) += report.filled_size;
        self.total_position += report.filled_size;
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct QuoteKey {
    market_id: MarketId,
    token_id: TokenId,
    side: Side,
}

#[derive(Clone, Debug)]
pub struct ManagedQuote {
    key: QuoteKey,
    pub decision: TradeDecision,
    pub placed_ts: DateTime<Utc>,
    pub expires_at: Option<DateTime<Utc>>,
    pub order_id: Option<OrderId>,
}

#[derive(Clone, Debug, Default)]
pub struct OrderManager {
    quotes: HashMap<QuoteKey, ManagedQuote>,
}

impl OrderManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn open_order_count(&self) -> usize {
        self.quotes.len()
    }

    pub fn open_quotes(&self) -> Vec<ManagedQuote> {
        self.quotes.values().cloned().collect()
    }

    pub fn reconcile(
        &self,
        market_id: &MarketId,
        decisions: &[TradeDecision],
        condition_id: Option<ConditionId>,
        now: DateTime<Utc>,
    ) -> Vec<TradeDecision> {
        if decisions
            .iter()
            .any(|decision| decision.action == polyedge_domain::DecisionAction::CancelAll)
        {
            let reason = decisions
                .first()
                .map_or("", |decision| decision.reason.as_str());
            return self.cancel_or_hold(market_id, reason, condition_id);
        }
        let place_decisions: Vec<_> = decisions
            .iter()
            .filter(|decision| decision.action == polyedge_domain::DecisionAction::Place)
            .cloned()
            .collect();
        let taker_decisions: Vec<_> = place_decisions
            .iter()
            .filter(|decision| {
                matches!(
                    decision.order_kind.as_ref(),
                    Some(OrderKind::Fak | OrderKind::Fok)
                )
            })
            .cloned()
            .collect();
        let maker_decisions: Vec<_> = place_decisions
            .iter()
            .filter(|decision| {
                matches!(
                    decision.order_kind.as_ref(),
                    Some(OrderKind::PostOnlyGtc | OrderKind::PostOnlyGtd)
                )
            })
            .cloned()
            .collect();
        if maker_decisions.is_empty() {
            if !self.market_quotes(market_id).is_empty() {
                let reason = decisions
                    .first()
                    .map_or("no desired maker quote", |decision| {
                        decision.reason.as_str()
                    });
                let mut actions = vec![cancel_all_decision(market_id, reason, condition_id)];
                actions.extend(taker_decisions);
                return actions;
            }
            if !taker_decisions.is_empty() {
                return taker_decisions;
            }
            let reason = decisions
                .first()
                .map_or("no decision", |decision| decision.reason.as_str());
            return vec![hold_decision(market_id, reason, condition_id)];
        }

        let desired_by_key: HashMap<QuoteKey, TradeDecision> = maker_decisions
            .iter()
            .filter_map(|decision| decision_key(decision).map(|key| (key, decision.clone())))
            .collect();
        let current_quotes = self.market_quotes(market_id);
        let mut needs_cancel = current_quotes
            .iter()
            .any(|quote| quote_is_expired(quote, now))
            || current_quotes
                .iter()
                .any(|quote| !desired_by_key.contains_key(&quote.key));
        for (key, desired) in &desired_by_key {
            if let Some(current) = self.quotes.get(key) {
                if !same_quote(&current.decision, desired) {
                    needs_cancel = true;
                    break;
                }
            }
        }
        let mut actions = Vec::new();
        if needs_cancel && !current_quotes.is_empty() {
            actions.push(cancel_all_decision(
                market_id,
                "cancel/replace maker quotes",
                condition_id.clone(),
            ));
        }
        if needs_cancel || current_quotes.is_empty() {
            actions.extend(maker_decisions);
            actions.extend(taker_decisions);
            return actions;
        }
        if !taker_decisions.is_empty() {
            return taker_decisions;
        }
        vec![hold_decision(
            market_id,
            "desired maker quotes already resting",
            condition_id,
        )]
    }

    pub fn on_execution_report(&mut self, decision: &TradeDecision, report: &ExecutionReport) {
        if decision.action == polyedge_domain::DecisionAction::CancelAll {
            self.clear_market(&decision.market_id);
            return;
        }
        if decision.action != polyedge_domain::DecisionAction::Place
            || !matches!(
                decision.order_kind.as_ref(),
                Some(OrderKind::PostOnlyGtc | OrderKind::PostOnlyGtd)
            )
            || report.status.ends_with("_error")
            || report.status.contains("rejected")
        {
            return;
        }
        let Some(key) = decision_key(decision) else {
            return;
        };
        let expires_at = decision
            .ttl_ms
            .map(|ttl| report.local_ts + Duration::milliseconds(ttl));
        self.quotes.insert(
            key.clone(),
            ManagedQuote {
                key,
                decision: decision.clone(),
                placed_ts: report.local_ts,
                expires_at,
                order_id: report.order_id.clone(),
            },
        );
    }

    pub fn clear_market(&mut self, market_id: &MarketId) {
        self.quotes.retain(|key, _| &key.market_id != market_id);
    }

    fn cancel_or_hold(
        &self,
        market_id: &MarketId,
        reason: &str,
        condition_id: Option<ConditionId>,
    ) -> Vec<TradeDecision> {
        if self.market_quotes(market_id).is_empty() {
            vec![hold_decision(market_id, reason, condition_id)]
        } else {
            vec![cancel_all_decision(market_id, reason, condition_id)]
        }
    }

    fn market_quotes(&self, market_id: &MarketId) -> Vec<ManagedQuote> {
        self.quotes
            .values()
            .filter(|quote| &quote.key.market_id == market_id)
            .cloned()
            .collect()
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct PaperFillStats {
    pub maker_fills: usize,
    pub prevented_not_live: usize,
    pub prevented_stale_book: usize,
    pub prevented_final_window: usize,
    pub prevented_market_inactive: usize,
    pub prevented_expired: usize,
    pub prevented_after_cancel: usize,
    pub last_fill_ts: Option<String>,
}

#[derive(Clone, Debug)]
pub struct RestingMakerOrder {
    pub order_id: OrderId,
    pub decision: TradeDecision,
    pub report: ExecutionReport,
}

#[derive(Clone, Debug)]
pub struct PaperFillEngine {
    settings: RuntimeSettings,
    pub stats: PaperFillStats,
}

impl PaperFillEngine {
    pub fn new(settings: RuntimeSettings) -> Self {
        Self {
            settings,
            stats: PaperFillStats::default(),
        }
    }

    pub fn on_book(
        &mut self,
        book: &BookState,
        markets_by_token: &BTreeMap<TokenId, MarketSpec>,
        resting_orders: &[RestingMakerOrder],
        tracked_order_ids: &BTreeSet<OrderId>,
        current_time: DateTime<Utc>,
    ) -> Vec<ExecutionReport> {
        if self.settings.paper.maker_fill_policy == "none" {
            return Vec::new();
        }
        let Some(market) = markets_by_token.get(&book.token_id) else {
            return Vec::new();
        };
        if resting_orders.is_empty() || book.best_ask().is_none() {
            return Vec::new();
        }
        let now = current_time.max(book.local_ts);
        let best_ask = book.best_ask().map(|level| level.price);
        if book.is_stale(self.settings.risk.max_book_age_ms, current_time) {
            self.stats.prevented_stale_book += resting_orders.len();
            return Vec::new();
        }
        let mut reports = Vec::new();
        for resting in resting_orders {
            if !tracked_order_ids.contains(&resting.order_id) {
                self.stats.prevented_after_cancel += 1;
                continue;
            }
            let decision = &resting.decision;
            if decision.side.as_ref() != Some(&Side::Buy)
                || !matches!(
                    decision.order_kind.as_ref(),
                    Some(OrderKind::PostOnlyGtc | OrderKind::PostOnlyGtd)
                )
            {
                continue;
            }
            let Some(price) = decision.price else {
                continue;
            };
            if !market_active(market, now) {
                self.stats.prevented_market_inactive += 1;
                continue;
            }
            if inside_final_window(market, now, self.settings.strategy.final_no_trade_seconds) {
                self.stats.prevented_final_window += 1;
                continue;
            }
            if !order_is_live(
                resting.report.local_ts,
                now,
                self.settings.paper.order_live_after_ms,
            ) {
                self.stats.prevented_not_live += 1;
                continue;
            }
            if order_is_expired(resting.report.local_ts, decision.ttl_ms, now) {
                self.stats.prevented_expired += 1;
                continue;
            }
            if best_ask.is_some_and(|ask| ask <= price) {
                self.stats.maker_fills += 1;
                self.stats.last_fill_ts =
                    Some(now.to_rfc3339_opts(chrono::SecondsFormat::Secs, true));
                let mut raw = BTreeMap::new();
                raw.insert(
                    "decision".to_owned(),
                    serde_json::to_value(decision).unwrap_or(serde_json::Value::Null),
                );
                reports.push(ExecutionReport {
                    order_id: Some(resting.order_id.clone()),
                    market_id: decision.market_id.clone(),
                    token_id: decision.token_id.clone(),
                    status: "paper_filled_maker".to_owned(),
                    filled_size: decision.size.unwrap_or(Decimal::ZERO),
                    avg_price: Some(price),
                    fee: Decimal::ZERO,
                    local_ts: now,
                    raw,
                });
            }
        }
        reports
    }
}

fn decimal_from_rounded_f64(value: f64, places: usize) -> Option<Decimal> {
    let factor = 10_f64.powi(i32::try_from(places).ok()?);
    let rounded = (value * factor).round() / factor;
    Decimal::from_str_exact(&format!("{rounded:.places$}")).ok()
}

fn decision_key(decision: &TradeDecision) -> Option<QuoteKey> {
    Some(QuoteKey {
        market_id: decision.market_id.clone(),
        token_id: decision.token_id.clone()?,
        side: decision.side.clone()?,
    })
}

fn same_quote(current: &TradeDecision, desired: &TradeDecision) -> bool {
    current.price == desired.price
        && current.size == desired.size
        && current.order_kind == desired.order_kind
        && current.post_only == desired.post_only
        && current.quote_amount == desired.quote_amount
}

fn quote_is_expired(quote: &ManagedQuote, now: DateTime<Utc>) -> bool {
    quote.expires_at.is_some_and(|expires_at| expires_at <= now)
}

fn cancel_all_decision(
    market_id: &MarketId,
    reason: &str,
    condition_id: Option<ConditionId>,
) -> TradeDecision {
    TradeDecision {
        action: polyedge_domain::DecisionAction::CancelAll,
        market_id: market_id.clone(),
        condition_id,
        token_id: None,
        outcome: None,
        side: None,
        price: None,
        size: None,
        quote_amount: None,
        order_kind: None,
        reason: reason.to_owned(),
        ttl_ms: None,
        expected_edge: None,
        post_only: false,
        tick_size: None,
        neg_risk: false,
    }
}

fn hold_decision(
    market_id: &MarketId,
    reason: &str,
    condition_id: Option<ConditionId>,
) -> TradeDecision {
    TradeDecision {
        action: polyedge_domain::DecisionAction::Hold,
        market_id: market_id.clone(),
        condition_id,
        token_id: None,
        outcome: None,
        side: None,
        price: None,
        size: None,
        quote_amount: None,
        order_kind: None,
        reason: reason.to_owned(),
        ttl_ms: None,
        expected_edge: None,
        post_only: false,
        tick_size: None,
        neg_risk: false,
    }
}

fn market_active(market: &MarketSpec, now: DateTime<Utc>) -> bool {
    market.start_ts <= now && now < market.end_ts
}

fn inside_final_window(
    market: &MarketSpec,
    now: DateTime<Utc>,
    final_no_trade_seconds: i64,
) -> bool {
    market.end_ts.signed_duration_since(now).num_seconds() <= final_no_trade_seconds
}

fn order_is_live(placed_ts: DateTime<Utc>, now: DateTime<Utc>, live_after_ms: i64) -> bool {
    now >= placed_ts + Duration::milliseconds(live_after_ms)
}

fn order_is_expired(placed_ts: DateTime<Utc>, ttl_ms: Option<i64>, now: DateTime<Utc>) -> bool {
    ttl_ms.is_some_and(|ttl| now >= placed_ts + Duration::milliseconds(ttl))
}
