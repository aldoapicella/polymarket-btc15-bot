use chrono::{DateTime, Duration, SecondsFormat, Utc};
use polyedge_engine::crypto_taker_fee_per_share;
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use thiserror::Error;

pub const REPLAY_BUFFER_BYTES: usize = 1024 * 1024;

#[derive(Debug, Error)]
pub enum ReportingError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("line {line}: {source}")]
    JsonLine {
        line: usize,
        source: serde_json::Error,
    },
}

#[derive(Clone, Debug)]
pub struct BacktestConfig {
    pub path: PathBuf,
    pub settlement_window_seconds: i64,
    pub exact_reference_source: String,
    pub max_book_age_ms: i64,
    pub final_no_trade_seconds: i64,
    pub paper_order_live_after_ms: i64,
}

impl BacktestConfig {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            settlement_window_seconds: 15,
            exact_reference_source: "polymarket_rtds_chainlink_btc_usd".to_owned(),
            max_book_age_ms: 1500,
            final_no_trade_seconds: 30,
            paper_order_live_after_ms: 250,
        }
    }
}

#[derive(Clone, Debug)]
struct ReplayMarket {
    market_id: String,
    market_slug: Option<String>,
    up_token_id: String,
    down_token_id: String,
    start_ts: DateTime<Utc>,
    end_ts: DateTime<Utc>,
    start_price: Option<Decimal>,
}

#[derive(Clone, Debug)]
struct ReplayOrder {
    market_id: String,
    token_id: String,
    outcome: String,
    side: String,
    price: Decimal,
    size: Decimal,
    order_kind: String,
    decision_ts: DateTime<Utc>,
    ttl_ms: Option<i64>,
    filled_size: Decimal,
    avg_price: Option<Decimal>,
    fee: Decimal,
    cancel_requested_ts: Option<DateTime<Utc>>,
    cancel_confirmed_ts: Option<DateTime<Utc>>,
    prevented_fill_ts: Option<DateTime<Utc>>,
}

impl ReplayOrder {
    fn is_filled(&self) -> bool {
        self.filled_size > Decimal::ZERO
    }

    fn is_cancelled(&self) -> bool {
        self.cancel_requested_ts.is_some()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BacktestResult {
    pub path: String,
    pub event_count: usize,
    pub markets_seen: usize,
    pub markets_with_start_price: usize,
    pub markets_settled: usize,
    pub decisions_seen: usize,
    pub orders_seen: usize,
    pub filled_orders: usize,
    pub gross_pnl: String,
    pub fees: String,
    pub net_pnl: String,
    pub replay_metrics: Value,
    pub notes: Vec<String>,
    pub market_results: Vec<Value>,
}

impl BacktestResult {
    pub fn as_value(&self) -> Value {
        serde_json::to_value(self)
            .unwrap_or_else(|_| json!({"error": "backtest serialization failed"}))
    }
}

#[derive(Clone, Debug)]
pub struct ReplayBacktester {
    config: BacktestConfig,
    markets: BTreeMap<String, ReplayMarket>,
    token_to_market: BTreeMap<String, (String, String)>,
    references: Vec<(DateTime<Utc>, Decimal)>,
    orders: Vec<ReplayOrder>,
    open_orders: BTreeSet<usize>,
    decisions_seen: usize,
    event_count: usize,
    notes: Vec<String>,
    cancel_decisions_seen: usize,
    cancel_execution_reports_seen: usize,
    orders_cancelled: usize,
    fills_after_cancel_prevented: usize,
    fills_prevented_not_live: usize,
    fills_prevented_stale_book: usize,
    fills_prevented_final_window: usize,
    fills_prevented_market_inactive: usize,
    fills_prevented_expired: usize,
}

impl ReplayBacktester {
    pub fn new(config: BacktestConfig) -> Self {
        Self {
            config,
            markets: BTreeMap::new(),
            token_to_market: BTreeMap::new(),
            references: Vec::new(),
            orders: Vec::new(),
            open_orders: BTreeSet::new(),
            decisions_seen: 0,
            event_count: 0,
            notes: Vec::new(),
            cancel_decisions_seen: 0,
            cancel_execution_reports_seen: 0,
            orders_cancelled: 0,
            fills_after_cancel_prevented: 0,
            fills_prevented_not_live: 0,
            fills_prevented_stale_book: 0,
            fills_prevented_final_window: 0,
            fills_prevented_market_inactive: 0,
            fills_prevented_expired: 0,
        }
    }

    pub fn run(&mut self) -> Result<BacktestResult, ReportingError> {
        let file = File::open(&self.config.path)?;
        self.run_reader(BufReader::with_capacity(REPLAY_BUFFER_BYTES, file))?;
        Ok(self.result())
    }

    pub fn run_reader<R>(&mut self, reader: R) -> Result<(), ReportingError>
    where
        R: BufRead,
    {
        self.run_reader_observing(reader, |_| {})
    }

    pub fn run_reader_observing<R, F>(
        &mut self,
        reader: R,
        mut observer: F,
    ) -> Result<(), ReportingError>
    where
        R: BufRead,
        F: FnMut(&Value),
    {
        for (line_number, line) in reader.lines().enumerate() {
            let line = line?;
            if line.trim().is_empty() {
                continue;
            }
            let event: Value =
                serde_json::from_str(&line).map_err(|source| ReportingError::JsonLine {
                    line: line_number + 1,
                    source,
                })?;
            observer(&event);
            self.event_count += 1;
            self.handle_event(&event);
        }
        Ok(())
    }

    pub fn run_events<I>(&mut self, events: I) -> BacktestResult
    where
        I: IntoIterator<Item = Value>,
    {
        for event in events {
            self.event_count += 1;
            self.handle_event(&event);
        }
        self.result()
    }

    pub fn replay_cost(&self) -> Decimal {
        self.orders
            .iter()
            .filter(|order| order.is_filled())
            .fold(Decimal::ZERO, |acc, order| {
                acc + order.avg_price.unwrap_or(order.price) * order.filled_size
            })
    }

    pub fn finish(&mut self) -> BacktestResult {
        self.result()
    }

    fn handle_event(&mut self, event: &Value) {
        let event_type = event
            .get("event_type")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let payload = event.get("payload").unwrap_or(&Value::Null);
        let recorded_ts = parse_datetime(event.get("recorded_ts")).unwrap_or_else(Utc::now);
        match event_type {
            "market" => self.handle_market(payload),
            "market_start_price" => self.handle_market_start_price(payload),
            "reference" => self.handle_reference(payload),
            "book" => self.handle_book(payload, recorded_ts),
            "decision" => self.handle_decision(payload, recorded_ts),
            "execution_report" => self.handle_execution_report(payload, recorded_ts),
            _ => {}
        }
    }

    fn handle_market(&mut self, payload: &Value) {
        let market_id = text(payload, "market_id");
        if market_id.is_empty() {
            return;
        }
        let Some(start_ts) = parse_datetime(payload.get("start_ts")) else {
            return;
        };
        let Some(end_ts) = parse_datetime(payload.get("end_ts")) else {
            return;
        };
        let existing_start = self
            .markets
            .get(&market_id)
            .and_then(|market| market.start_price);
        let market = ReplayMarket {
            market_id: market_id.clone(),
            market_slug: optional_text(payload, "market_slug"),
            up_token_id: text(payload, "up_token_id"),
            down_token_id: text(payload, "down_token_id"),
            start_ts,
            end_ts,
            start_price: decimal(payload.get("start_price")).or(existing_start),
        };
        self.token_to_market.insert(
            market.up_token_id.clone(),
            (market_id.clone(), "up".to_owned()),
        );
        self.token_to_market.insert(
            market.down_token_id.clone(),
            (market_id.clone(), "down".to_owned()),
        );
        self.markets.insert(market_id, market);
    }

    fn handle_market_start_price(&mut self, payload: &Value) {
        let market_id = text(payload, "market_id");
        let price = decimal(payload.get("start_price"));
        if let (Some(market), Some(price)) = (self.markets.get_mut(&market_id), price) {
            market.start_price = Some(price);
        }
    }

    fn handle_reference(&mut self, payload: &Value) {
        if text(payload, "source") != self.config.exact_reference_source
            || bool_value(payload, "stale")
        {
            return;
        }
        if let (Some(price), Some(source_ts)) = (
            decimal(payload.get("price")),
            parse_datetime(payload.get("source_ts")),
        ) {
            self.references.push((source_ts, price));
        }
    }

    fn handle_book(&mut self, payload: &Value, recorded_ts: DateTime<Utc>) {
        let token_id = text(payload, "token_id");
        let Some(best_ask) = best_ask(payload) else {
            return;
        };
        let book_ts = parse_datetime(payload.get("local_ts")).unwrap_or(recorded_ts);
        if book_is_stale(book_ts, recorded_ts, self.config.max_book_age_ms) {
            self.fills_prevented_stale_book += self
                .open_orders
                .iter()
                .filter(|index| self.orders[**index].token_id == token_id)
                .count();
            return;
        }
        for order in &mut self.orders {
            if order.token_id != token_id
                || order.is_filled()
                || !order.is_cancelled()
                || order.prevented_fill_ts.is_some()
            {
                continue;
            }
            if would_fill_on_best_ask(order, best_ask) {
                order.prevented_fill_ts = Some(recorded_ts);
                self.fills_after_cancel_prevented += 1;
            }
        }
        let open: Vec<usize> = self.open_orders.iter().copied().collect();
        for index in open {
            if self.orders[index].token_id != token_id || self.orders[index].is_cancelled() {
                continue;
            }
            if !self.order_can_fill(index, recorded_ts) {
                continue;
            }
            if would_fill_on_best_ask(&self.orders[index], best_ask) {
                self.fill_order(index, self.orders[index].price, recorded_ts, true);
                self.open_orders.remove(&index);
            }
        }
    }

    fn order_can_fill(&mut self, index: usize, recorded_ts: DateTime<Utc>) -> bool {
        let order = &self.orders[index];
        let Some(market) = self.markets.get(&order.market_id) else {
            self.fills_prevented_market_inactive += 1;
            return false;
        };
        if !(market.start_ts <= recorded_ts && recorded_ts < market.end_ts) {
            self.fills_prevented_market_inactive += 1;
            return false;
        }
        if market
            .end_ts
            .signed_duration_since(recorded_ts)
            .num_seconds()
            <= self.config.final_no_trade_seconds
        {
            self.fills_prevented_final_window += 1;
            return false;
        }
        if recorded_ts
            < order.decision_ts + Duration::milliseconds(self.config.paper_order_live_after_ms)
        {
            self.fills_prevented_not_live += 1;
            return false;
        }
        if order.order_kind.starts_with("post_only")
            && order.ttl_ms.is_some_and(|ttl_ms| {
                recorded_ts >= order.decision_ts + Duration::milliseconds(ttl_ms)
            })
        {
            self.fills_prevented_expired += 1;
            return false;
        }
        true
    }

    fn handle_decision(&mut self, payload: &Value, recorded_ts: DateTime<Utc>) {
        self.decisions_seen += 1;
        let action = text(payload, "action");
        if action == "cancel_all" {
            self.handle_cancel_all_decision(payload, recorded_ts);
            return;
        }
        if action != "place" {
            return;
        }
        let token_id = text(payload, "token_id");
        let market_id = text(payload, "market_id");
        let Some(price) = decimal(payload.get("price")) else {
            return;
        };
        let Some(size) = decimal(payload.get("size")) else {
            return;
        };
        if token_id.is_empty() || market_id.is_empty() {
            return;
        }
        let order = ReplayOrder {
            market_id,
            token_id,
            outcome: text(payload, "outcome"),
            side: text(payload, "side"),
            price,
            size,
            order_kind: text(payload, "order_kind"),
            decision_ts: recorded_ts,
            ttl_ms: payload.get("ttl_ms").and_then(Value::as_i64),
            filled_size: Decimal::ZERO,
            avg_price: None,
            fee: Decimal::ZERO,
            cancel_requested_ts: None,
            cancel_confirmed_ts: None,
            prevented_fill_ts: None,
        };
        self.orders.push(order);
        let index = self.orders.len() - 1;
        if matches!(self.orders[index].order_kind.as_str(), "fak" | "fok") {
            self.fill_order(index, self.orders[index].price, recorded_ts, false);
        } else if self.orders[index].order_kind.starts_with("post_only") {
            self.open_orders.insert(index);
        }
    }

    fn handle_cancel_all_decision(&mut self, payload: &Value, recorded_ts: DateTime<Utc>) {
        self.cancel_decisions_seen += 1;
        let market_id = text(payload, "market_id");
        let open: Vec<usize> = self.open_orders.iter().copied().collect();
        for index in open {
            if !market_id.is_empty() && self.orders[index].market_id != market_id {
                continue;
            }
            self.orders[index].cancel_requested_ts = Some(recorded_ts);
            self.orders[index].cancel_confirmed_ts = Some(recorded_ts);
            self.open_orders.remove(&index);
            self.orders_cancelled += 1;
        }
    }

    fn handle_execution_report(&mut self, payload: &Value, recorded_ts: DateTime<Utc>) {
        let status = text(payload, "status");
        if status != "paper_cancelled" && status != "live_cancel_all_submitted" {
            return;
        }
        self.cancel_execution_reports_seen += 1;
        let market_id = text(payload, "market_id");
        let token_id = text(payload, "token_id");
        let open: Vec<usize> = self.open_orders.iter().copied().collect();
        for index in open {
            if !market_id.is_empty() && self.orders[index].market_id != market_id {
                continue;
            }
            if !token_id.is_empty() && self.orders[index].token_id != token_id {
                continue;
            }
            if self.orders[index].cancel_requested_ts.is_none() {
                self.orders[index].cancel_requested_ts = Some(recorded_ts);
            }
            self.orders[index].cancel_confirmed_ts = Some(recorded_ts);
            self.open_orders.remove(&index);
            self.orders_cancelled += 1;
        }
    }

    fn fill_order(&mut self, index: usize, price: Decimal, fill_ts: DateTime<Utc>, maker: bool) {
        let order = &mut self.orders[index];
        order.filled_size = order.size;
        order.avg_price = Some(price);
        if !maker {
            order.fee = crypto_taker_fee_per_share(price).unwrap_or(Decimal::ZERO) * order.size;
        }
        let _ = fill_ts;
    }

    fn result(&mut self) -> BacktestResult {
        let mut market_results = Vec::new();
        let mut gross = Decimal::ZERO;
        let mut fees = Decimal::ZERO;
        let mut settled_count = 0;
        for market in self.markets.values() {
            let start_price = market.start_price;
            let final_price = self.settlement_price(market);
            let settled = start_price.is_some() && final_price.is_some();
            if settled {
                settled_count += 1;
            }
            let market_orders: Vec<&ReplayOrder> = self
                .orders
                .iter()
                .filter(|order| order.market_id == market.market_id && order.is_filled())
                .collect();
            let mut market_gross = Decimal::ZERO;
            let mut market_fees = Decimal::ZERO;
            let mut winning_outcome = None;
            if let (Some(start_price), Some(final_price)) = (start_price, final_price) {
                let winner = if final_price >= start_price {
                    "up"
                } else {
                    "down"
                };
                winning_outcome = Some(winner.to_owned());
                for order in &market_orders {
                    let payout = if order.outcome == winner {
                        order.filled_size
                    } else {
                        Decimal::ZERO
                    };
                    let cost = order.avg_price.unwrap_or(order.price) * order.filled_size;
                    market_gross += payout - cost;
                    market_fees += order.fee;
                }
            }
            gross += market_gross;
            fees += market_fees;
            market_results.push(json!({
                "market_id": market.market_id,
                "market_slug": market.market_slug,
                "start_ts": ts(market.start_ts),
                "end_ts": ts(market.end_ts),
                "start_price": start_price.map(|value| value.to_string()),
                "final_price": final_price.map(|value| value.to_string()),
                "winning_outcome": winning_outcome,
                "filled_orders": market_orders.len(),
                "gross_pnl": market_gross.to_string(),
                "fees": market_fees.to_string(),
                "net_pnl": (market_gross - market_fees).to_string()
            }));
        }
        if self.references.is_empty() {
            self.notes
                .push("no usable Polymarket RTDS Chainlink reference events found".to_owned());
        }
        if !self
            .markets
            .values()
            .any(|market| market.start_price.is_some())
        {
            self.notes.push(
                "no market_start_price events or market payload start prices found".to_owned(),
            );
        }
        if self.orders.is_empty() {
            self.notes
                .push("no place decisions found; observer may not have crossed a captured market start yet".to_owned());
        }
        BacktestResult {
            path: self.config.path.to_string_lossy().into_owned(),
            event_count: self.event_count,
            markets_seen: self.markets.len(),
            markets_with_start_price: self
                .markets
                .values()
                .filter(|market| market.start_price.is_some())
                .count(),
            markets_settled: settled_count,
            decisions_seen: self.decisions_seen,
            orders_seen: self.orders.len(),
            filled_orders: self.orders.iter().filter(|order| order.is_filled()).count(),
            gross_pnl: gross.to_string(),
            fees: fees.to_string(),
            net_pnl: (gross - fees).to_string(),
            replay_metrics: json!({
                "placed_orders": self.orders.len(),
                "cancel_decisions_seen": self.cancel_decisions_seen,
                "cancel_execution_reports_seen": self.cancel_execution_reports_seen,
                "orders_cancelled": self.orders_cancelled,
                "open_orders_remaining": self.open_orders.len(),
                "fills_after_cancel_prevented": self.fills_after_cancel_prevented,
                "fills_prevented_not_live": self.fills_prevented_not_live,
                "fills_prevented_stale_book": self.fills_prevented_stale_book,
                "fills_prevented_final_window": self.fills_prevented_final_window,
                "fills_prevented_market_inactive": self.fills_prevented_market_inactive,
                "fills_prevented_expired": self.fills_prevented_expired
            }),
            notes: self.notes.clone(),
            market_results,
        }
    }

    fn settlement_price(&self, market: &ReplayMarket) -> Option<Decimal> {
        let lower = market.end_ts - Duration::seconds(self.config.settlement_window_seconds);
        let upper = market.end_ts + Duration::seconds(self.config.settlement_window_seconds);
        let candidates: Vec<_> = self
            .references
            .iter()
            .filter(|(event_ts, _)| lower <= *event_ts && *event_ts <= upper)
            .copied()
            .collect();
        if candidates.is_empty() {
            return None;
        }
        candidates
            .iter()
            .filter(|(event_ts, _)| *event_ts >= market.end_ts)
            .min_by_key(|(event_ts, _)| *event_ts)
            .or_else(|| candidates.iter().max_by_key(|(event_ts, _)| *event_ts))
            .map(|(_, price)| *price)
    }
}

pub fn run_backtest(path: &Path) -> Result<BacktestResult, ReportingError> {
    ReplayBacktester::new(BacktestConfig::new(path)).run()
}

pub fn build_pnl_report(path: &Path) -> Result<Value, ReportingError> {
    let source = json!({"type": "local_jsonl", "path": path.to_string_lossy()});
    let mut actual = ActualPaperAccumulator::default();
    let mut backtester = ReplayBacktester::new(BacktestConfig::new(path));
    let file = File::open(path)?;
    backtester.run_reader_observing(
        BufReader::with_capacity(REPLAY_BUFFER_BYTES, file),
        |event| actual.observe(event),
    )?;
    let replay = backtester.result();
    let actual_summary = actual.summary(&replay.market_results);
    let replay_cost = backtester.replay_cost();
    let replay_net = decimal_from_string(&replay.net_pnl);
    let runtime_vs_replay = runtime_vs_replay(&actual_summary, &replay);
    let replay_market_level = market_level_statistics(&replay.market_results);
    Ok(json!({
        "source": source,
        "summary": {
            "actual_paper_state": state(actual_summary.get("net_pnl").and_then(Value::as_str).unwrap_or("0")),
            "actual_paper_net_pnl": actual_summary["net_pnl"],
            "replay_estimate_state": state(&replay.net_pnl),
            "replay_estimate_net_pnl": replay.net_pnl,
            "replay_estimate_roi_on_cost": ratio(replay_net, replay_cost),
            "replay_market_level_mean_pnl": replay_market_level["market_level_mean_pnl"],
            "replay_market_level_95ci_low": replay_market_level["market_level_95ci_low"],
            "replay_market_level_95ci_high": replay_market_level["market_level_95ci_high"],
            "replay_profitability_statistically_proven_95ci": replay_market_level["profitability_statistically_proven_95ci"],
            "runtime_minus_replay_fills": runtime_vs_replay["runtime_minus_replay_fills"],
            "runtime_minus_replay_pnl": runtime_vs_replay["runtime_minus_replay_pnl"]
        },
        "runtime_vs_replay": runtime_vs_replay,
        "actual_paper": merge_objects(json!({
            "meaning": "Runtime paper ledger built only from execution_report events with positive filled_size. Maker fills appear here only when the runtime paper fill engine emits paper_filled_maker.",
            "runtime_fill_policy": "unknown"
        }), actual_summary),
        "replay_estimate": merge_objects(json!({
            "meaning": "Offline cancellation-aware replay over recorded market, decision, book, and Chainlink reference events.",
            "replay_fill_policy": "touch_after_cancel_aware",
            "assumption": "Post-only maker orders are treated as filled when the captured best ask touches or crosses the quote while the replay order is open. cancel_all decisions remove eligible open replay orders. Replay also enforces the configured quote-live delay, TTL, active-market window, final no-trade window, and stale-book guard. Maker fees are modeled as zero; unsettled markets are excluded from PnL.",
            "notional_cost": replay_cost.to_string(),
            "market_level_statistics": replay_market_level
        }), replay.as_value())
    }))
}

#[derive(Default)]
struct ActualPaperAccumulator {
    status_counts: BTreeMap<String, usize>,
    reports_seen: usize,
    filled_reports: Vec<Value>,
}

impl ActualPaperAccumulator {
    fn observe(&mut self, event: &Value) {
        if event.get("event_type").and_then(Value::as_str) != Some("execution_report") {
            return;
        }
        let payload = event.get("payload").unwrap_or(&Value::Null);
        self.reports_seen += 1;
        let status = text(payload, "status");
        *self
            .status_counts
            .entry(if status.is_empty() {
                "unknown".to_owned()
            } else {
                status
            })
            .or_insert(0) += 1;
        if decimal(payload.get("filled_size")).unwrap_or(Decimal::ZERO) <= Decimal::ZERO {
            return;
        }
        self.filled_reports.push(payload.clone());
    }

    fn summary(&self, replay_market_results: &[Value]) -> Value {
        let stats = market_level_statistics(&actual_market_results(replay_market_results));
        json!({
            "execution_reports_seen": self.reports_seen,
            "status_counts": self.status_counts,
            "filled_reports": self.filled_reports.len(),
            "settled_filled_reports": 0,
            "filled_shares": "0",
            "notional_cost": "0",
            "gross_pnl": "0",
            "fees": "0",
            "net_pnl": "0",
            "roi_on_cost": Value::Null,
            "market_level_statistics": stats
        })
    }
}

pub fn iter_jsonl(path: &Path) -> Result<Vec<Value>, ReportingError> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut events = Vec::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        events.push(serde_json::from_str(&line)?);
    }
    Ok(events)
}

pub fn count_jsonl_events(path: &Path) -> Result<usize, ReportingError> {
    if !path.exists() {
        return Ok(0);
    }
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut count = 0;
    for line in reader.lines() {
        if !line?.trim().is_empty() {
            count += 1;
        }
    }
    Ok(count)
}

fn parse_datetime(value: Option<&Value>) -> Option<DateTime<Utc>> {
    let text = value?.as_str()?;
    DateTime::parse_from_rfc3339(text)
        .ok()
        .map(|dt| dt.with_timezone(&Utc))
}

fn decimal(value: Option<&Value>) -> Option<Decimal> {
    match value? {
        Value::String(text) => Decimal::from_str_exact(text).ok(),
        Value::Number(number) => Decimal::from_str_exact(&number.to_string()).ok(),
        _ => None,
    }
}

fn decimal_from_string(value: &str) -> Decimal {
    Decimal::from_str_exact(value).unwrap_or(Decimal::ZERO)
}

fn text(payload: &Value, key: &str) -> String {
    payload
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn optional_text(payload: &Value, key: &str) -> Option<String> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
}

fn bool_value(payload: &Value, key: &str) -> bool {
    payload.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn best_ask(payload: &Value) -> Option<Decimal> {
    payload
        .get("asks")
        .and_then(Value::as_array)?
        .iter()
        .filter_map(|item| decimal(item.get("price")))
        .min()
}

fn book_is_stale(book_ts: DateTime<Utc>, recorded_ts: DateTime<Utc>, max_book_age_ms: i64) -> bool {
    recorded_ts
        .signed_duration_since(book_ts)
        .num_microseconds()
        .map_or(0.0, |micros| (micros.max(0) as f64) / 1000.0)
        > max_book_age_ms as f64
}

fn would_fill_on_best_ask(order: &ReplayOrder, best_ask: Decimal) -> bool {
    order.side == "buy" && best_ask <= order.price
}

fn ts(value: DateTime<Utc>) -> String {
    value.to_rfc3339_opts(SecondsFormat::Secs, false)
}

fn state(value: &str) -> &'static str {
    let pnl = decimal_from_string(value);
    match pnl.cmp(&Decimal::ZERO) {
        std::cmp::Ordering::Greater => "winning",
        std::cmp::Ordering::Less => "losing",
        std::cmp::Ordering::Equal => "flat",
    }
}

fn ratio(numerator: Decimal, denominator: Decimal) -> Value {
    if denominator == Decimal::ZERO {
        Value::Null
    } else {
        json!((numerator / denominator).to_string())
    }
}

fn runtime_vs_replay(actual: &Value, replay: &BacktestResult) -> Value {
    let runtime_fills = actual
        .get("filled_reports")
        .and_then(Value::as_u64)
        .unwrap_or(0) as i64;
    let replay_fills = replay.filled_orders as i64;
    let runtime_net = actual
        .get("net_pnl")
        .and_then(Value::as_str)
        .map(decimal_from_string)
        .unwrap_or(Decimal::ZERO);
    let replay_net = decimal_from_string(&replay.net_pnl);
    json!({
        "runtime_filled_reports": runtime_fills,
        "replay_filled_orders": replay_fills,
        "runtime_minus_replay_fills": runtime_fills - replay_fills,
        "runtime_net_pnl": runtime_net.to_string(),
        "replay_net_pnl": replay_net.to_string(),
        "runtime_minus_replay_pnl": (runtime_net - replay_net).to_string()
    })
}

fn actual_market_results(replay_market_results: &[Value]) -> Vec<Value> {
    replay_market_results
        .iter()
        .map(|row| {
            let mut row = row.clone();
            if let Some(object) = row.as_object_mut() {
                object.insert("gross_pnl".to_owned(), json!("0"));
                object.insert("fees".to_owned(), json!("0"));
                object.insert("net_pnl".to_owned(), json!("0"));
            }
            row
        })
        .collect()
}

fn market_level_statistics(market_results: &[Value]) -> Value {
    let values: Vec<Decimal> = market_results
        .iter()
        .filter(|row| {
            row.get("winning_outcome")
                .is_some_and(|value| !value.is_null())
        })
        .filter_map(|row| {
            row.get("net_pnl")
                .and_then(Value::as_str)
                .map(decimal_from_string)
        })
        .collect();
    let n = values.len();
    let mean = if n == 0 {
        None
    } else {
        Some(values.iter().copied().sum::<Decimal>() / Decimal::from(n))
    };
    let std = sample_std(&values, mean);
    let standard_error =
        std.and_then(|value| Decimal::from_f64_retain(value.to_f64()? / (n as f64).sqrt()));
    let ci_low = match (mean, standard_error) {
        (Some(mean), Some(se)) => Some(mean - Decimal::new(196, 2) * se),
        _ => None,
    };
    let ci_high = match (mean, standard_error) {
        (Some(mean), Some(se)) => Some(mean + Decimal::new(196, 2) * se),
        _ => None,
    };
    json!({
        "sample_unit": "settled_market_net_pnl",
        "markets_count": n,
        "market_level_mean_pnl": mean.map(|value| value.to_string()),
        "market_level_std_pnl": std.map(|value| value.to_string()),
        "market_level_standard_error": standard_error.map(|value| value.to_string()),
        "market_level_95ci_low": ci_low.map(|value| value.to_string()),
        "market_level_95ci_high": ci_high.map(|value| value.to_string()),
        "confidence_interval_includes_zero": match (ci_low, ci_high) {
            (Some(low), Some(high)) => Value::Bool(low <= Decimal::ZERO && Decimal::ZERO <= high),
            _ => Value::Null
        },
        "profitability_statistically_proven_95ci": ci_low.map(|low| low > Decimal::ZERO),
        "required_markets_for_0_05_precision": required_markets_for_precision(std, 0.05),
        "required_markets_for_0_10_precision": required_markets_for_precision(std, 0.10),
        "required_markets_to_detect_current_mean": required_markets_to_detect_current_mean(std, mean),
        "required_markets_method": "precision uses (1.96 * sample_std / desired_margin)^2; detect_current_mean uses 7.84 * (sample_std / abs(mean_pnl))^2."
    })
}

fn sample_std(values: &[Decimal], mean: Option<Decimal>) -> Option<Decimal> {
    if values.len() < 2 {
        return None;
    }
    let mean = mean?.to_f64()?;
    let variance = values
        .iter()
        .filter_map(Decimal::to_f64)
        .map(|value| (value - mean).powi(2))
        .sum::<f64>()
        / (values.len() - 1) as f64;
    Decimal::from_f64_retain(variance.sqrt())
}

fn required_markets_for_precision(std: Option<Decimal>, desired_margin: f64) -> Value {
    let Some(std) = std.and_then(|value| value.to_f64()) else {
        return Value::Null;
    };
    if std == 0.0 {
        return json!(1);
    }
    json!(((1.96 * std / desired_margin).powi(2)).ceil() as i64)
}

fn required_markets_to_detect_current_mean(std: Option<Decimal>, mean: Option<Decimal>) -> Value {
    let (Some(std), Some(mean)) = (
        std.and_then(|value| value.to_f64()),
        mean.and_then(|value| value.to_f64()),
    ) else {
        return Value::Null;
    };
    if mean == 0.0 {
        return Value::Null;
    }
    if std == 0.0 {
        return json!(1);
    }
    json!((7.84 * (std / mean.abs()).powi(2)).ceil() as i64)
}

fn merge_objects(left: Value, right: Value) -> Value {
    let mut merged = serde_json::Map::new();
    if let Value::Object(object) = left {
        merged.extend(object);
    }
    if let Value::Object(object) = right {
        merged.extend(object);
    }
    Value::Object(merged)
}
