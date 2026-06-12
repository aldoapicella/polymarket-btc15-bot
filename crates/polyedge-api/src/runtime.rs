mod chart;
mod chart_history;
mod recorder;
mod reference;
mod view;

use chart::chart_sample_from_data;
use chart_history::{point_bucket_ms, should_persist, spawn_persist, ChartPersistenceSample};
use chrono::{DateTime, Utc};
use polyedge_config::{ExecutionMode, RuntimeSettings};
use polyedge_domain::{
    BookState, DecisionAction, ExecutionReport, MarketId, MarketSpec, ReferencePrice, RuntimeEvent,
    TokenId, TradeDecision,
};
use polyedge_engine::{
    LogReturnFairValueModel, MakerFirstStrategy, OrderManager, PaperFillEngine, RestingMakerOrder,
    RiskManager,
};
use polyedge_execution::{ExecutionClient, PaperExecutionClient};
use polyedge_feeds::{self, FeedEvent, FeedName};
use recorder::RuntimeRecorder;
use reference::ReferenceAggregator;
use rust_decimal::Decimal;
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;
use tokio::sync::{broadcast, mpsc, Mutex, RwLock};
use tokio::task::JoinHandle;
use tracing::{debug, error, info, warn};

const RECENT_LIMIT: usize = 1_000;
const HISTORY_LIMIT: usize = 500;
const CHART_HISTORY_LIMIT: usize = 2_000;
const RECORDER_BATCH_LIMIT: usize = 500;

#[derive(Clone)]
pub struct RuntimeController {
    inner: Arc<RuntimeInner>,
}

struct RuntimeInner {
    settings: RuntimeSettings,
    data: RwLock<RuntimeData>,
    engine: Mutex<RuntimeEngine>,
    recorder: Arc<StdMutex<RuntimeRecorder>>,
    recorder_tx: std_mpsc::Sender<RuntimeEvent>,
    recorder_metrics: Arc<RecorderMetrics>,
    broadcaster: broadcast::Sender<RuntimeEvent>,
    started: AtomicBool,
}

#[derive(Debug, Default)]
struct RecorderMetrics {
    queued: AtomicUsize,
    enqueued_total: AtomicU64,
    persisted_total: AtomicU64,
    failed_total: AtomicU64,
    batches_total: AtomicU64,
    last_batch_size: AtomicUsize,
}

impl RecorderMetrics {
    fn snapshot(&self) -> Value {
        json!({
            "queued": self.queued.load(Ordering::Relaxed),
            "enqueued_total": self.enqueued_total.load(Ordering::Relaxed),
            "persisted_total": self.persisted_total.load(Ordering::Relaxed),
            "failed_total": self.failed_total.load(Ordering::Relaxed),
            "batches_total": self.batches_total.load(Ordering::Relaxed),
            "last_batch_size": self.last_batch_size.load(Ordering::Relaxed)
        })
    }
}

#[derive(Clone, Debug)]
struct RuntimeData {
    started_at: DateTime<Utc>,
    paused: bool,
    pause_reason: Option<String>,
    paused_at: Option<DateTime<Utc>>,
    kill_switch: bool,
    markets: BTreeMap<MarketId, MarketSpec>,
    books: BTreeMap<TokenId, BookState>,
    reference: Option<ReferencePrice>,
    fair_values: BTreeMap<MarketId, Value>,
    chart_samples: BTreeMap<MarketId, VecDeque<Value>>,
    chart_last_persisted_ms: BTreeMap<MarketId, i64>,
    decisions: VecDeque<TradeDecision>,
    execution_reports: VecDeque<ExecutionReport>,
    recent_events: VecDeque<RuntimeEvent>,
    settled_markets: Vec<MarketId>,
    feed_status: BTreeMap<String, Value>,
    feed_events: usize,
    runtime_events: usize,
    drop_counts: BTreeMap<String, usize>,
}

struct RuntimeEngine {
    fair_model: LogReturnFairValueModel,
    strategy: MakerFirstStrategy,
    risk: RiskManager,
    order_manager: OrderManager,
    execution: PaperExecutionClient,
    paper_fill_engine: PaperFillEngine,
    reference_aggregator: ReferenceAggregator,
    last_volatility_update_key: Option<(String, DateTime<Utc>, Decimal)>,
}

impl RuntimeController {
    pub fn new(settings: RuntimeSettings) -> Self {
        let (broadcaster, _) = broadcast::channel(1_000);
        let data = RuntimeData {
            started_at: Utc::now(),
            paused: false,
            pause_reason: None,
            paused_at: None,
            kill_switch: false,
            markets: BTreeMap::new(),
            books: BTreeMap::new(),
            reference: None,
            fair_values: BTreeMap::new(),
            chart_samples: BTreeMap::new(),
            chart_last_persisted_ms: BTreeMap::new(),
            decisions: VecDeque::new(),
            execution_reports: VecDeque::new(),
            recent_events: VecDeque::new(),
            settled_markets: Vec::new(),
            feed_status: BTreeMap::new(),
            feed_events: 0,
            runtime_events: 0,
            drop_counts: BTreeMap::new(),
        };
        let engine = RuntimeEngine {
            fair_model: LogReturnFairValueModel::new(settings.clone()),
            strategy: MakerFirstStrategy::new(settings.clone()),
            risk: RiskManager::new(settings.clone()),
            order_manager: OrderManager::new(),
            execution: PaperExecutionClient::new(),
            paper_fill_engine: PaperFillEngine::new(settings.clone()),
            reference_aggregator: ReferenceAggregator::default(),
            last_volatility_update_key: None,
        };
        let recorder = Arc::new(StdMutex::new(RuntimeRecorder::new(&settings)));
        let recorder_metrics = Arc::new(RecorderMetrics::default());
        let (recorder_tx, recorder_rx) = std_mpsc::channel();
        spawn_recorder_worker(
            Arc::clone(&recorder),
            recorder_rx,
            Arc::clone(&recorder_metrics),
        );
        Self {
            inner: Arc::new(RuntimeInner {
                settings,
                data: RwLock::new(data),
                engine: Mutex::new(engine),
                recorder,
                recorder_tx,
                recorder_metrics,
                broadcaster,
                started: AtomicBool::new(false),
            }),
        }
    }

    pub fn start_if_configured(&self) {
        if !self.inner.settings.deploy.run_bot_on_startup {
            return;
        }
        if self.inner.started.swap(true, Ordering::SeqCst) {
            return;
        }
        let (sender, receiver) = mpsc::channel(10_000);
        self.spawn_feed_event_loop(receiver);
        self.spawn_discovery_loop();
        self.spawn_strategy_loop();
        self.spawn_market_feed_loop(sender.clone());
        self.spawn_rtds_loop(sender.clone());
        self.spawn_chainlink_http_loop(sender.clone());
        if self.inner.settings.target.enable_direct_binance_book_ticker {
            self.spawn_binance_loop(sender);
        } else {
            info!("Direct Binance bookTicker feed disabled by configuration");
        }
        info!("Rust PolyEdge runtime started in paper mode");
    }

    pub fn subscribe(&self) -> broadcast::Receiver<RuntimeEvent> {
        self.inner.broadcaster.subscribe()
    }

    pub async fn pause(&self, reason: Option<String>) -> Value {
        {
            let mut data = self.inner.data.write().await;
            data.paused = true;
            data.paused_at = Some(Utc::now());
            data.pause_reason = reason.clone();
        }
        self.cancel_active_markets(reason.unwrap_or_else(|| "operator pause".to_owned()))
            .await;
        json!({
            "control": self.control_status().await,
            "audit_version": format!("rust-control-{}", Utc::now().timestamp_micros())
        })
    }

    pub async fn resume(&self, _reason: Option<String>) -> Value {
        {
            let mut data = self.inner.data.write().await;
            data.paused = false;
            data.paused_at = None;
            data.pause_reason = None;
        }
        self.publish_only("control_state_changed", self.control_status().await)
            .await;
        json!({
            "control": self.control_status().await,
            "audit_version": format!("rust-control-{}", Utc::now().timestamp_micros())
        })
    }

    pub async fn set_kill_switch(&self, enabled: bool, reason: Option<String>) -> Value {
        {
            let mut data = self.inner.data.write().await;
            data.kill_switch = enabled;
        }
        self.record_event(
            "control_state_changed",
            json!({"kill_switch": enabled, "reason": reason}),
            None,
            None,
        )
        .await;
        json!({
            "enabled": enabled,
            "audit_version": format!("rust-kill-switch-{}", Utc::now().timestamp_micros())
        })
    }

    async fn control_status(&self) -> Value {
        let data = self.inner.data.read().await;
        json!({
            "paused": data.paused,
            "paused_at": data.paused_at,
            "pause_reason": data.pause_reason
        })
    }

    fn spawn_feed_event_loop(&self, mut receiver: mpsc::Receiver<FeedEvent>) -> JoinHandle<()> {
        let runtime = self.clone();
        tokio::spawn(async move {
            while let Some(event) = receiver.recv().await {
                runtime.handle_feed_event(event).await;
            }
        })
    }

    fn spawn_discovery_loop(&self) -> JoinHandle<()> {
        let runtime = self.clone();
        tokio::spawn(async move {
            loop {
                runtime.set_feed_status("discovery", "running", None).await;
                let settings = runtime.inner.settings.clone();
                let result = tokio::task::spawn_blocking(move || {
                    polyedge_feeds::discover_markets(&settings)
                })
                .await;
                match result {
                    Ok(Ok(markets)) => {
                        runtime.replace_markets(markets).await;
                        runtime.set_feed_status("discovery", "ok", None).await;
                    }
                    Ok(Err(error)) => {
                        runtime
                            .feed_error(FeedName::Discovery, error.to_string())
                            .await;
                    }
                    Err(error) => {
                        runtime
                            .feed_error(FeedName::Discovery, error.to_string())
                            .await;
                    }
                }
                tokio::time::sleep(Duration::from_secs_f64(
                    runtime
                        .inner
                        .settings
                        .target
                        .discovery_interval_seconds
                        .max(2.0),
                ))
                .await;
            }
        })
    }

    fn spawn_strategy_loop(&self) -> JoinHandle<()> {
        let runtime = self.clone();
        tokio::spawn(async move {
            loop {
                runtime.evaluate_once().await;
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        })
    }

    fn spawn_market_feed_loop(&self, sender: mpsc::Sender<FeedEvent>) -> JoinHandle<()> {
        let runtime = self.clone();
        tokio::spawn(async move {
            loop {
                let token_ids = runtime.market_token_ids().await;
                if token_ids.is_empty() {
                    runtime
                        .set_feed_status("polymarket_clob_market", "waiting_for_markets", None)
                        .await;
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    continue;
                }
                runtime
                    .set_feed_status("polymarket_clob_market", "connecting", None)
                    .await;
                match polyedge_feeds::run_market_feed(
                    runtime.inner.settings.clone(),
                    token_ids,
                    sender.clone(),
                )
                .await
                {
                    Ok(()) => {
                        runtime
                            .set_feed_status("polymarket_clob_market", "disconnected", None)
                            .await;
                    }
                    Err(error) => {
                        runtime
                            .feed_error(FeedName::PolymarketClobMarket, error.to_string())
                            .await;
                    }
                }
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        })
    }

    fn spawn_rtds_loop(&self, sender: mpsc::Sender<FeedEvent>) -> JoinHandle<()> {
        let runtime = self.clone();
        tokio::spawn(async move {
            loop {
                runtime
                    .set_feed_status("polymarket_rtds", "connecting", None)
                    .await;
                match polyedge_feeds::run_rtds_feed(runtime.inner.settings.clone(), sender.clone())
                    .await
                {
                    Ok(()) => {
                        runtime
                            .set_feed_status("polymarket_rtds", "disconnected", None)
                            .await;
                    }
                    Err(error) => {
                        runtime
                            .feed_error(FeedName::PolymarketRtdsChainlink, error.to_string())
                            .await;
                    }
                }
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        })
    }

    fn spawn_chainlink_http_loop(&self, sender: mpsc::Sender<FeedEvent>) -> JoinHandle<()> {
        let runtime = self.clone();
        tokio::spawn(async move {
            loop {
                let settings = runtime.inner.settings.clone();
                if settings.target.chainlink_reference_url.is_none() {
                    runtime
                        .set_feed_status("chainlink_http", "disabled", None)
                        .await;
                    tokio::time::sleep(Duration::from_secs(30)).await;
                    continue;
                }
                let result = tokio::task::spawn_blocking(move || {
                    polyedge_feeds::fetch_chainlink_reference(&settings)
                })
                .await;
                match result {
                    Ok(Ok(Some(reference))) => {
                        let _ = sender.send(FeedEvent::Reference(reference)).await;
                        runtime.set_feed_status("chainlink_http", "ok", None).await;
                    }
                    Ok(Ok(None)) => {
                        runtime
                            .set_feed_status("chainlink_http", "no_data", None)
                            .await
                    }
                    Ok(Err(error)) => {
                        runtime
                            .feed_error(FeedName::ChainlinkHttp, error.to_string())
                            .await
                    }
                    Err(error) => {
                        runtime
                            .feed_error(FeedName::ChainlinkHttp, error.to_string())
                            .await
                    }
                }
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        })
    }

    fn spawn_binance_loop(&self, sender: mpsc::Sender<FeedEvent>) -> JoinHandle<()> {
        let runtime = self.clone();
        tokio::spawn(async move {
            loop {
                runtime
                    .set_feed_status("binance_book_ticker", "connecting", None)
                    .await;
                match polyedge_feeds::run_binance_book_ticker_feed(
                    runtime.inner.settings.clone(),
                    sender.clone(),
                )
                .await
                {
                    Ok(()) => {
                        runtime
                            .set_feed_status("binance_book_ticker", "disconnected", None)
                            .await;
                    }
                    Err(error) => {
                        runtime
                            .feed_error(FeedName::BinanceBookTicker, error.to_string())
                            .await;
                    }
                }
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        })
    }

    async fn handle_feed_event(&self, event: FeedEvent) {
        {
            let mut data = self.inner.data.write().await;
            data.feed_events += 1;
        }
        match event {
            FeedEvent::Reference(reference) => self.handle_reference(reference).await,
            FeedEvent::Book(book) => self.handle_book(book).await,
            FeedEvent::Error {
                source, message, ..
            } => self.feed_error(source, message).await,
            FeedEvent::Heartbeat { source, .. } => {
                self.set_feed_status(&format!("{source:?}"), "ok", None)
                    .await;
            }
        }
    }

    async fn replace_markets(&self, markets: Vec<MarketSpec>) {
        let mut data = self.inner.data.write().await;
        let existing = data.markets.clone();
        data.markets.clear();
        for mut market in markets {
            if market.start_price.is_none() {
                if let Some(prior) = existing.get(&market.market_id) {
                    if let Some(start_price) = prior.start_price {
                        market = market.with_start_price(start_price);
                    }
                }
            }
            let payload = serde_json::to_value(&market).unwrap_or(Value::Null);
            data.markets.insert(market.market_id.clone(), market);
            drop(data);
            self.record_event("market", payload, Some("market_discovered"), None)
                .await;
            data = self.inner.data.write().await;
        }
    }

    async fn handle_reference(&self, reference: ReferencePrice) {
        let composite = {
            let mut engine = self.inner.engine.lock().await;
            let composite = engine
                .reference_aggregator
                .update(reference, &self.inner.settings);
            if composite.exact_resolution_source {
                let key = (
                    composite.source.clone(),
                    composite.source_ts,
                    composite.price,
                );
                if engine.last_volatility_update_key.as_ref() != Some(&key) {
                    engine.fair_model.update_volatility(&composite);
                    engine.last_volatility_update_key = Some(key);
                }
            }
            composite
        };
        {
            let mut data = self.inner.data.write().await;
            data.reference = Some(composite.clone());
        }
        self.capture_market_start_prices(&composite).await;
        self.settle_finished_markets(&composite).await;
        self.record_event("reference", &composite, Some("reference_update"), None)
            .await;
    }

    async fn handle_book(&self, book: BookState) {
        let market = {
            let mut data = self.inner.data.write().await;
            data.books.insert(book.token_id.clone(), book.clone());
            markets_by_token_from_data(&data)
                .get(&book.token_id)
                .cloned()
        };
        let publish_payload = book_summary(&book, market.as_ref());
        self.record_event(
            "book",
            &book,
            Some("book_update_summary"),
            Some(publish_payload),
        )
        .await;
        if let Some(market) = market {
            self.push_market_chart_sample(&market.market_id).await;
        }
        self.handle_paper_fills(&book).await;
    }

    async fn handle_paper_fills(&self, book: &BookState) {
        let markets_by_token = {
            let data = self.inner.data.read().await;
            markets_by_token_from_data(&data)
        };
        let reports = {
            let mut engine = self.inner.engine.lock().await;
            let resting: Vec<_> = engine
                .execution
                .resting_for_token(&book.token_id)
                .into_iter()
                .map(|resting| RestingMakerOrder {
                    order_id: resting.order_id,
                    decision: resting.decision,
                    report: resting.report,
                })
                .collect();
            let tracked = engine.order_manager.open_order_ids();
            let candidate_reports = engine.paper_fill_engine.on_book(
                book,
                &markets_by_token,
                &resting,
                &tracked,
                Utc::now(),
            );
            let mut filled = Vec::new();
            for report in candidate_reports {
                let Some(order_id) = report.order_id.clone() else {
                    continue;
                };
                let avg_price = report.avg_price.unwrap_or(Decimal::ZERO);
                if let Some(mut actual) =
                    engine
                        .execution
                        .fill_maker_order(&order_id, avg_price, report.local_ts)
                {
                    actual.status = "paper_filled_maker".to_owned();
                    engine.order_manager.on_fill(&actual);
                    engine.risk.open_order_count = engine.order_manager.open_order_count();
                    engine.risk.on_execution_report(&actual);
                    filled.push(actual);
                }
            }
            filled
        };
        for report in reports {
            self.record_execution_report(report, true).await;
        }
    }

    async fn evaluate_once(&self) {
        let (reference, markets, books, paused, kill_switch) = {
            let data = self.inner.data.read().await;
            (
                data.reference.clone(),
                active_markets(&data)
                    .into_iter()
                    .cloned()
                    .collect::<Vec<_>>(),
                data.books.clone(),
                data.paused,
                data.kill_switch,
            )
        };
        let Some(reference) = reference else {
            return;
        };
        if paused {
            return;
        }
        for market in markets {
            let decisions = {
                let mut engine = self.inner.engine.lock().await;
                engine.risk.open_order_count = engine.order_manager.open_order_count();
                let now = Utc::now();
                let Some(fair_value) = engine
                    .fair_model
                    .compute(&market, &reference, now, None, None)
                else {
                    continue;
                };
                {
                    let mut data = self.inner.data.write().await;
                    data.fair_values.insert(
                        market.market_id.clone(),
                        serde_json::to_value(&fair_value).unwrap_or(Value::Null),
                    );
                }
                self.push_market_chart_sample(&market.market_id).await;
                self.record_event("fair_value", &fair_value, Some("fair_value_update"), None)
                    .await;
                let raw_decisions = engine.strategy.evaluate(&market, &fair_value, &books);
                let assessment =
                    engine
                        .risk
                        .assess_market(&market, &reference, &books, now, kill_switch);
                let risk_decisions =
                    engine
                        .risk
                        .filter_decisions(&raw_decisions, &market, &assessment);
                engine.order_manager.reconcile(
                    &market.market_id,
                    &risk_decisions,
                    Some(market.condition_id.clone()),
                    now,
                )
            };

            for decision in decisions {
                self.push_decision(decision.clone()).await;
                if matches!(
                    decision.action,
                    DecisionAction::Place | DecisionAction::CancelAll
                ) {
                    let report = {
                        let mut engine = self.inner.engine.lock().await;
                        match engine.execution.submit(&decision).await {
                            Ok(report) => {
                                engine.order_manager.on_execution_report(&decision, &report);
                                engine.risk.open_order_count =
                                    engine.order_manager.open_order_count();
                                engine.risk.on_execution_report(&report);
                                Some(report)
                            }
                            Err(error) => {
                                error!("paper execution failed: {error}");
                                None
                            }
                        }
                    };
                    if let Some(report) = report {
                        self.record_execution_report(report, false).await;
                    }
                }
            }
        }
    }

    async fn push_decision(&self, decision: TradeDecision) {
        {
            let mut data = self.inner.data.write().await;
            data.decisions.push_back(decision.clone());
            truncate(&mut data.decisions, HISTORY_LIMIT);
        }
        self.record_event("decision", &decision, None, None).await;
    }

    async fn record_execution_report(&self, report: ExecutionReport, publish_fill: bool) {
        {
            let mut data = self.inner.data.write().await;
            data.execution_reports.push_back(report.clone());
            truncate(&mut data.execution_reports, HISTORY_LIMIT);
        }
        self.record_event("execution_report", &report, None, None)
            .await;
        self.push_market_chart_sample(&report.market_id).await;
        if publish_fill && report.status == "paper_filled_maker" {
            self.publish_only("paper_fill", &report).await;
        }
    }

    async fn push_market_chart_sample(&self, market_id: &MarketId) {
        let persistence = {
            let mut data = self.inner.data.write().await;
            let Some(market) = data.markets.get(market_id).cloned() else {
                return;
            };
            let point = chart_sample_from_data(&market, &data, Utc::now());
            let bucket_ms = point_bucket_ms(&point);
            let sample_count = {
                let samples = data.chart_samples.entry(market_id.clone()).or_default();
                samples.push_back(point.clone());
                truncate(samples, CHART_HISTORY_LIMIT);
                samples.len()
            };
            match bucket_ms {
                Some(bucket_ms)
                    if should_persist(
                        data.chart_last_persisted_ms.get(market_id).copied(),
                        bucket_ms,
                    ) =>
                {
                    data.chart_last_persisted_ms
                        .insert(market_id.clone(), bucket_ms);
                    Some(ChartPersistenceSample::new(market, point, sample_count))
                }
                _ => None,
            }
        };
        if let Some(sample) = persistence {
            spawn_persist(self.inner.settings.clone(), sample);
        };
    }

    async fn capture_market_start_prices(&self, reference: &ReferencePrice) {
        if reference.stale || !reference.exact_resolution_source {
            return;
        }
        let grace = self.inner.settings.target.start_price_capture_grace_seconds;
        let mut updates = Vec::new();
        {
            let mut data = self.inner.data.write().await;
            for market in data.markets.values_mut() {
                if market.start_price.is_some() {
                    continue;
                }
                let seconds_after_start = reference
                    .source_ts
                    .signed_duration_since(market.start_ts)
                    .num_microseconds()
                    .map_or(-1.0, |micros| micros as f64 / 1_000_000.0);
                if seconds_after_start >= 0.0 && seconds_after_start <= grace {
                    *market = market.clone().with_start_price(reference.price);
                    updates.push(json!({
                        "market_id": market.market_id,
                        "market_slug": market.market_slug,
                        "start_price": reference.price.to_string(),
                        "reference_source": reference.source,
                        "reference_source_ts": reference.source_ts
                    }));
                }
            }
        }
        for update in updates {
            self.record_event("market_start_price", update, None, None)
                .await;
        }
    }

    async fn settle_finished_markets(&self, reference: &ReferencePrice) {
        if reference.stale || !reference.exact_resolution_source {
            return;
        }
        let markets = {
            let data = self.inner.data.read().await;
            data.markets.values().cloned().collect::<Vec<_>>()
        };
        for market in markets {
            if market.start_price.is_none() || reference.source_ts < market.end_ts {
                continue;
            }
            {
                let data = self.inner.data.read().await;
                if data.settled_markets.contains(&market.market_id) {
                    continue;
                }
            }
            let start_price = market.start_price.unwrap_or(Decimal::ZERO);
            let winning_outcome = if reference.price >= start_price {
                "up"
            } else {
                "down"
            };
            let cleared_position = {
                let mut engine = self.inner.engine.lock().await;
                engine.order_manager.clear_market(&market.market_id);
                engine.execution.clear_market(&market.market_id);
                engine.risk.clear_market(&market.market_id)
            };
            {
                let mut data = self.inner.data.write().await;
                data.settled_markets.push(market.market_id.clone());
            }
            self.record_event(
                "paper_settlement",
                json!({
                    "market_id": market.market_id,
                    "market_slug": market.market_slug,
                    "start_ts": market.start_ts,
                    "end_ts": market.end_ts,
                    "start_price": start_price.to_string(),
                    "final_price": reference.price.to_string(),
                    "winning_outcome": winning_outcome,
                    "reference_source": reference.source,
                    "reference_source_ts": reference.source_ts,
                    "cleared_position": cleared_position.to_string()
                }),
                None,
                None,
            )
            .await;
        }
    }

    async fn cancel_active_markets(&self, reason: String) {
        let markets = {
            let data = self.inner.data.read().await;
            active_markets(&data)
                .into_iter()
                .cloned()
                .collect::<Vec<_>>()
        };
        for market in markets {
            let decision = TradeDecision {
                action: DecisionAction::CancelAll,
                market_id: market.market_id.clone(),
                condition_id: Some(market.condition_id.clone()),
                token_id: None,
                outcome: None,
                side: None,
                price: None,
                size: None,
                quote_amount: None,
                order_kind: None,
                reason: reason.clone(),
                ttl_ms: None,
                expected_edge: None,
                post_only: false,
                tick_size: None,
                neg_risk: false,
            };
            self.push_decision(decision.clone()).await;
            let report = {
                let mut engine = self.inner.engine.lock().await;
                match engine.execution.submit(&decision).await {
                    Ok(report) => {
                        engine.order_manager.on_execution_report(&decision, &report);
                        Some(report)
                    }
                    Err(error) => {
                        warn!("cancel during pause failed: {error}");
                        None
                    }
                }
            };
            if let Some(report) = report {
                self.record_execution_report(report, false).await;
            }
        }
    }

    async fn record_event<P>(
        &self,
        event_type: &str,
        payload: P,
        publish_type: Option<&str>,
        publish_payload: Option<Value>,
    ) where
        P: Serialize,
    {
        let data = serde_json::to_value(payload).unwrap_or(Value::Null);
        let event = RuntimeEvent {
            event_type: event_type.to_owned(),
            ts: Utc::now(),
            data: data.clone(),
        };
        self.inner
            .recorder_metrics
            .queued
            .fetch_add(1, Ordering::Relaxed);
        self.inner
            .recorder_metrics
            .enqueued_total
            .fetch_add(1, Ordering::Relaxed);
        let recorder_queue_failed = self.inner.recorder_tx.send(event.clone()).is_err();
        if recorder_queue_failed {
            saturating_sub_atomic(&self.inner.recorder_metrics.queued, 1);
            self.inner
                .recorder_metrics
                .failed_total
                .fetch_add(1, Ordering::Relaxed);
        }
        {
            let mut state = self.inner.data.write().await;
            state.runtime_events += 1;
            if recorder_queue_failed {
                *state
                    .drop_counts
                    .entry("recorder_queue_send_error".to_owned())
                    .or_insert(0) += 1;
                warn!("runtime recorder queue is unavailable; event was not persisted");
            }
            state.recent_events.push_back(event.clone());
            truncate(&mut state.recent_events, RECENT_LIMIT);
        }
        let publish_event = RuntimeEvent {
            event_type: publish_type.unwrap_or(event_type).to_owned(),
            ts: event.ts,
            data: publish_payload.unwrap_or(data),
        };
        if let Err(error) = self.inner.broadcaster.send(publish_event) {
            debug!("runtime event had no subscribers: {error}");
        }
    }

    async fn publish_only<P>(&self, event_type: &str, payload: P)
    where
        P: Serialize,
    {
        let event = RuntimeEvent {
            event_type: event_type.to_owned(),
            ts: Utc::now(),
            data: serde_json::to_value(payload).unwrap_or(Value::Null),
        };
        let _ = self.inner.broadcaster.send(event);
    }

    async fn set_feed_status(&self, name: &str, status: &str, message: Option<String>) {
        let mut data = self.inner.data.write().await;
        data.feed_status.insert(
            name.to_owned(),
            json!({
                "status": status,
                "message": message,
                "updated_at": Utc::now()
            }),
        );
    }

    async fn feed_error(&self, source: FeedName, message: String) {
        let source_text = format!("{source:?}");
        self.set_feed_status(&source_text, "error", Some(message.clone()))
            .await;
        self.record_event(
            "feed_error",
            json!({
                "feed": source_text,
                "error": message
            }),
            None,
            None,
        )
        .await;
    }

    async fn market_token_ids(&self) -> Vec<TokenId> {
        let data = self.inner.data.read().await;
        data.markets
            .values()
            .flat_map(|market| [market.up_token_id.clone(), market.down_token_id.clone()])
            .collect()
    }
}

fn spawn_recorder_worker(
    recorder: Arc<StdMutex<RuntimeRecorder>>,
    receiver: std_mpsc::Receiver<RuntimeEvent>,
    metrics: Arc<RecorderMetrics>,
) {
    if let Err(error) = std::thread::Builder::new()
        .name("polyedge-recorder".to_owned())
        .spawn(move || {
            while let Ok(event) = receiver.recv() {
                let mut batch = vec![event];
                while batch.len() < RECORDER_BATCH_LIMIT {
                    match receiver.try_recv() {
                        Ok(event) => batch.push(event),
                        Err(std_mpsc::TryRecvError::Empty) => break,
                        Err(std_mpsc::TryRecvError::Disconnected) => break,
                    }
                }
                metrics.batches_total.fetch_add(1, Ordering::Relaxed);
                metrics
                    .last_batch_size
                    .store(batch.len(), Ordering::Relaxed);
                let result = match recorder.lock() {
                    Ok(mut recorder) => recorder.record_batch(&batch),
                    Err(error) => {
                        warn!("runtime recorder lock poisoned: {error}");
                        break;
                    }
                };
                saturating_sub_atomic(&metrics.queued, batch.len());
                match result {
                    Ok(()) => {
                        metrics
                            .persisted_total
                            .fetch_add(batch.len() as u64, Ordering::Relaxed);
                    }
                    Err(error) => {
                        metrics
                            .failed_total
                            .fetch_add(batch.len() as u64, Ordering::Relaxed);
                        warn!("runtime recorder failed: {error}");
                    }
                }
            }
        })
    {
        warn!("failed to start runtime recorder worker: {error}");
    }
}

fn saturating_sub_atomic(value: &AtomicUsize, amount: usize) {
    let _ = value.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
        Some(current.saturating_sub(amount))
    });
}

fn active_markets(data: &RuntimeData) -> Vec<&MarketSpec> {
    let now = Utc::now();
    data.markets
        .values()
        .filter(|market| market.start_ts <= now && now < market.end_ts)
        .collect()
}

fn markets_by_token_from_data(data: &RuntimeData) -> BTreeMap<TokenId, MarketSpec> {
    let mut markets_by_token = BTreeMap::new();
    for market in data.markets.values() {
        markets_by_token.insert(market.up_token_id.clone(), market.clone());
        markets_by_token.insert(market.down_token_id.clone(), market.clone());
    }
    markets_by_token
}

fn book_summary(book: &BookState, market: Option<&MarketSpec>) -> Value {
    let mut value = json!({
        "token_id": book.token_id,
        "best_bid": book.best_bid(),
        "best_ask": book.best_ask(),
        "last_trade_price": book.last_trade_price.map(|price| price.to_string()),
        "exchange_ts": book.exchange_ts,
        "local_ts": book.local_ts,
        "book_hash": book.book_hash
    });
    if let (Some(market), Value::Object(map)) = (market, &mut value) {
        map.insert("market_id".to_owned(), json!(market.market_id));
        if book.token_id == market.up_token_id {
            map.insert("outcome".to_owned(), json!("up"));
        } else if book.token_id == market.down_token_id {
            map.insert("outcome".to_owned(), json!("down"));
        }
    }
    value
}

fn feed_summary(data: &RuntimeData) -> &'static str {
    if data.feed_status.values().any(|status| {
        status
            .get("status")
            .and_then(Value::as_str)
            .is_some_and(|status| status == "ok" || status == "running" || status == "connecting")
    }) {
        "running"
    } else {
        "starting"
    }
}

fn report_status(shadow_only: bool) -> Value {
    json!({
        "running_job": Value::Null,
        "known_jobs": 0,
        "store": {
            "backend_impl": "rust",
            "shadow_only": shadow_only
        }
    })
}

fn execution_mode(settings: &RuntimeSettings) -> &'static str {
    match settings.live.execution_mode {
        ExecutionMode::Paper => "paper",
        ExecutionMode::Live => "live",
    }
}

fn truncate<T>(values: &mut VecDeque<T>, limit: usize) {
    while values.len() > limit {
        values.pop_front();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use std::thread;
    use std::time::Duration as StdDuration;

    #[test]
    fn recorder_worker_serializes_burst_without_try_lock_drops() {
        let dir = std::env::temp_dir().join(format!(
            "polyedge-recorder-worker-{}-{}",
            std::process::id(),
            Utc::now().timestamp_micros()
        ));
        let path = dir.join("events.jsonl");
        let recorder = Arc::new(StdMutex::new(RuntimeRecorder::new_for_path(path.clone())));
        let metrics = Arc::new(RecorderMetrics::default());
        let (sender, receiver) = std_mpsc::channel();
        spawn_recorder_worker(Arc::clone(&recorder), receiver, Arc::clone(&metrics));

        for index in 0..100 {
            metrics.queued.fetch_add(1, Ordering::Relaxed);
            metrics.enqueued_total.fetch_add(1, Ordering::Relaxed);
            sender
                .send(RuntimeEvent {
                    event_type: "book".to_owned(),
                    ts: Utc::now(),
                    data: json!({ "index": index }),
                })
                .unwrap();
        }
        drop(sender);

        for _ in 0..100 {
            let lines = fs::read_to_string(&path)
                .map(|text| text.lines().count())
                .unwrap_or_default();
            if lines == 100 {
                break;
            }
            thread::sleep(StdDuration::from_millis(10));
        }

        let text = fs::read_to_string(&path).unwrap();
        assert_eq!(text.lines().count(), 100);
        assert_eq!(recorder.lock().unwrap().status(false)["error_count"], 0);
        assert_eq!(metrics.snapshot()["queued"], 0);
        assert_eq!(metrics.snapshot()["enqueued_total"], 100);
        assert_eq!(metrics.snapshot()["persisted_total"], 100);
        assert_eq!(metrics.snapshot()["failed_total"], 0);
        let _ = fs::remove_dir_all(dir);
    }
}
