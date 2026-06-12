use super::{
    active_markets, execution_mode, feed_summary, report_status, RuntimeController, RuntimeData,
    RuntimeRecorder,
};
use chrono::{SecondsFormat, Utc};
use polyedge_domain::{ExecutionReport, MarketId, MarketSpec, RuntimeEvent, TradeDecision};
use rust_decimal::Decimal;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::atomic::Ordering;

impl RuntimeController {
    pub async fn health(&self) -> Value {
        let data = self.inner.data.read().await;
        json!({
            "ok": true,
            "backend_impl": "rust",
            "shadow_only": false,
            "runtime_active": self.inner.started.load(Ordering::SeqCst),
            "execution_mode": execution_mode(&self.inner.settings),
            "kill_switch": data.kill_switch,
            "reports": report_status(false)
        })
    }

    pub async fn status(&self) -> Value {
        let data = self.inner.data.read().await;
        let engine = self.inner.engine.lock().await;
        let now = Utc::now();
        let recorder_status = self.recorder_status();
        json!({
            "app": self.inner.settings.deploy.app_name,
            "backend_impl": "rust",
            "shadow_only": false,
            "git_sha": option_env!("GIT_SHA").unwrap_or("unknown"),
            "version": env!("CARGO_PKG_VERSION"),
            "execution_mode": execution_mode(&self.inner.settings),
            "started_at": data.started_at.to_rfc3339_opts(SecondsFormat::Secs, true),
            "now": now.to_rfc3339_opts(SecondsFormat::Secs, true),
            "uptime": now.signed_duration_since(data.started_at).num_seconds(),
            "markets": data.markets.len(),
            "tradeable_markets": active_markets(&data).len(),
            "books": data.books.len(),
            "tracked_open_orders": engine.order_manager.open_order_count(),
            "control": {
                "paused": data.paused,
                "paused_at": data.paused_at.map(|ts| ts.to_rfc3339_opts(SecondsFormat::Secs, true)),
                "pause_reason": data.pause_reason
            },
            "kill_switch": data.kill_switch,
            "task_health": {
                "api": "ok",
                "runtime_loop": if self.inner.started.load(Ordering::SeqCst) { "running" } else { "not_started" },
                "feeds": feed_summary(&data)
            },
            "queue_depths": {
                "feed_events": 0,
                "runtime_events": 0,
                "recorder": 0
            },
            "drop_counts": data.drop_counts,
            "feed_status": data.feed_status,
            "recorder_status": recorder_status.clone(),
            "event_bus_subscribers": self.inner.broadcaster.receiver_count(),
            "paper_fill": {
                "paper_fill_policy": self.inner.settings.paper.maker_fill_policy,
                "paper_order_live_after_ms": self.inner.settings.paper.order_live_after_ms,
                "paper_open_resting_orders": engine.execution.resting_orders.len(),
                "paper_maker_fills": engine.paper_fill_engine.stats.maker_fills
            },
            "paper_fill_stats": engine.paper_fill_engine.stats,
            "heartbeat_status": {
                "enabled": self.inner.settings.live.enable_heartbeat,
                "status": "disabled_in_rust_paper"
            },
            "live_heartbeat": Value::Null,
            "recorder": recorder_status,
            "reference": data.reference,
            "reports": report_status(false),
            "latest_decisions": latest_chronological(&data.decisions, 20),
            "latest_execution_reports": latest_chronological(&data.execution_reports, 20)
        })
    }

    pub async fn snapshot(&self) -> Value {
        json!({
            "status": self.status().await,
            "current_market": self.current_market().await,
            "markets": self.markets().await,
            "open_orders": self.orders().await,
            "fills": self.fills().await,
            "latest_decisions": self.decisions().await,
            "latest_execution_reports": self.execution_reports().await
        })
    }

    pub async fn markets(&self) -> Vec<Value> {
        let data = self.inner.data.read().await;
        let mut markets: Vec<_> = data
            .markets
            .values()
            .map(|market| self.market_summary_from_data(market, &data))
            .collect();
        markets.sort_by_key(|value| {
            value
                .get("start_ts")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned()
        });
        markets
    }

    pub async fn current_market(&self) -> Value {
        let data = self.inner.data.read().await;
        let current = active_markets(&data)
            .into_iter()
            .min_by_key(|market| market.end_ts)
            .map(|market| self.market_summary_from_data(market, &data));
        current.unwrap_or(Value::Null)
    }

    pub async fn market_detail(&self, market_id: &str) -> Option<Value> {
        let data = self.inner.data.read().await;
        let market = data.markets.get(&MarketId::new(market_id.to_owned()))?;
        let related_decisions = latest_matching(&data.decisions, 100, |decision| {
            decision.market_id == market.market_id
        });
        let related_reports = latest_matching(&data.execution_reports, 100, |report| {
            report.market_id == market.market_id
        });
        Some(json!({
            "market": self.market_summary_from_data(market, &data),
            "fair_value": data.fair_values.get(&market.market_id).cloned().unwrap_or(Value::Null),
            "books": {
                "up": data.books.get(&market.up_token_id),
                "down": data.books.get(&market.down_token_id)
            },
            "decisions": related_decisions,
            "execution_reports": related_reports
        }))
    }

    pub async fn market_chart(&self, market_id: &str, range: &str) -> Option<Value> {
        let data = self.inner.data.read().await;
        let market = data.markets.get(&MarketId::new(market_id.to_owned()))?;
        let mut points = data
            .chart_samples
            .get(&market.market_id)
            .map(|samples| samples.iter().cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        let stored_count = points.len();
        filter_chart_range(&mut points, range);
        Some(json!({
            "source": "rust_runtime_memory",
            "market_id": market.market_id,
            "range": range,
            "points": points,
            "domain": [
                market.start_ts.timestamp_millis(),
                market.end_ts.timestamp_millis()
            ],
            "summary": {
                "sample_count": stored_count
            }
        }))
    }

    pub async fn orders(&self) -> Vec<Value> {
        let engine = self.inner.engine.lock().await;
        engine
            .order_manager
            .open_quotes()
            .into_iter()
            .map(|quote| {
                json!({
                    "market_id": quote.decision.market_id,
                    "token_id": quote.decision.token_id,
                    "side": quote.decision.side,
                    "placed_ts": quote.placed_ts,
                    "expires_at": quote.expires_at,
                    "order_id": quote.order_id,
                    "decision": quote.decision
                })
            })
            .collect()
    }

    pub async fn fills(&self) -> Vec<ExecutionReport> {
        let data = self.inner.data.read().await;
        latest_matching(&data.execution_reports, 200, |report| {
            report.filled_size > Decimal::ZERO
        })
    }

    pub async fn decisions(&self) -> Vec<TradeDecision> {
        let data = self.inner.data.read().await;
        latest_chronological(&data.decisions, 200)
    }

    pub async fn execution_reports(&self) -> Vec<ExecutionReport> {
        let data = self.inner.data.read().await;
        latest_chronological(&data.execution_reports, 200)
    }

    pub async fn recent_events(
        &self,
        limit: usize,
        event_type: Option<String>,
        market_id: Option<String>,
    ) -> Vec<RuntimeEvent> {
        let data = self.inner.data.read().await;
        latest_matching(&data.recent_events, limit, |event| {
            event_type
                .as_ref()
                .is_none_or(|target| &event.event_type == target)
                && market_id.as_ref().is_none_or(|target| {
                    event
                        .data
                        .get("market_id")
                        .and_then(Value::as_str)
                        .is_some_and(|value| value == target)
                })
        })
    }

    fn recorder_status(&self) -> Value {
        match self.inner.recorder.try_lock() {
            Ok(recorder) => recorder.status(false),
            Err(_) => RuntimeRecorder::busy_status(),
        }
    }

    fn market_summary_from_data(&self, market: &MarketSpec, data: &RuntimeData) -> Value {
        let now = Utc::now();
        let mut value = serde_json::to_value(market).unwrap_or(Value::Null);
        if let Value::Object(map) = &mut value {
            map.insert(
                "is_active".to_owned(),
                Value::Bool(market.start_ts <= now && now < market.end_ts),
            );
            map.insert(
                "is_tradeable".to_owned(),
                Value::Bool(market.is_tradeable()),
            );
            map.insert(
                "fair_value".to_owned(),
                data.fair_values
                    .get(&market.market_id)
                    .cloned()
                    .unwrap_or(Value::Null),
            );
        }
        value
    }
}

fn filter_chart_range(points: &mut Vec<Value>, range: &str) {
    let Some(window_ms) = chart_window_ms(range) else {
        return;
    };
    let Some(last_bucket) = points.iter().filter_map(point_bucket).max() else {
        return;
    };
    let cutoff = last_bucket - window_ms;
    points.retain(|point| point_bucket(point).is_some_and(|bucket| bucket >= cutoff));
}

fn chart_window_ms(range: &str) -> Option<i64> {
    match range {
        "1m" => Some(60_000),
        "5m" => Some(5 * 60_000),
        _ => None,
    }
}

fn point_bucket(point: &Value) -> Option<i64> {
    point.get("bucket").and_then(|value| match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse().ok(),
        _ => None,
    })
}

fn latest_chronological<T: Clone>(values: &VecDeque<T>, limit: usize) -> Vec<T> {
    latest_matching(values, limit, |_| true)
}

fn latest_matching<T, F>(values: &VecDeque<T>, limit: usize, mut predicate: F) -> Vec<T>
where
    T: Clone,
    F: FnMut(&T) -> bool,
{
    values
        .iter()
        .rev()
        .filter(|item| predicate(*item))
        .take(limit)
        .cloned()
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect()
}
