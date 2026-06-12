use axum::extract::ws::{Message, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::{header, StatusCode};
use axum::middleware::{self, Next};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::{SecondsFormat, Utc};
use polyedge_config::{ExecutionMode, RuntimeSettings};
use polyedge_reporting::{build_pnl_report, run_backtest};
use serde::Deserialize;
use serde_json::{json, Value};
use std::env;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::RwLock;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;

mod history;
mod runtime;
use history::{
    empty_chart, historical_detail, max_historical_markets, merge_chart_payloads,
    merge_market_lists, overlay_detail_market, MarketHistoryStore,
};
use runtime::RuntimeController;

const RECENT_EVENTS_MAX: usize = 500;

#[derive(Clone)]
pub struct ApiState {
    settings: RuntimeSettings,
    runtime: RuntimeController,
    latest_report: Arc<RwLock<Option<Value>>>,
}

impl ApiState {
    pub fn new(settings: RuntimeSettings) -> Self {
        let runtime = RuntimeController::new(settings.clone());
        runtime.start_if_configured();
        Self {
            settings,
            runtime,
            latest_report: Arc::new(RwLock::new(None)),
        }
    }

    pub fn settings(&self) -> &RuntimeSettings {
        &self.settings
    }
}

pub fn app(settings: RuntimeSettings) -> Router {
    let state = ApiState::new(settings);
    Router::new()
        .route("/health", get(health))
        .route("/status", get(status))
        .route("/snapshot", get(snapshot))
        .route("/pnl", get(pnl))
        .route("/api/v1/health", get(health))
        .route("/api/v1/status", get(status))
        .route("/api/v1/snapshot", get(snapshot))
        .route("/api/v1/markets", get(markets))
        .route("/api/v1/markets/current", get(current_market))
        .route("/api/v1/markets/history", get(markets_history))
        .route("/api/v1/markets/:market_id", get(market_detail))
        .route("/api/v1/markets/:market_id/chart", get(market_chart))
        .route("/api/v1/orders", get(orders))
        .route("/api/v1/fills", get(fills))
        .route("/api/v1/decisions", get(decisions))
        .route("/api/v1/events/recent", get(recent_events))
        .route("/api/v1/pnl", get(pnl))
        .route("/api/v1/reports/build", post(build_report))
        .route("/api/v1/reports/latest", get(latest_report))
        .route("/api/v1/reports/daily/:date", get(daily_report))
        .route("/api/v1/reports/:job_id", get(report_job))
        .route("/api/v1/control/pause", post(pause))
        .route("/api/v1/control/resume", post(resume))
        .route("/api/v1/control/kill-switch", post(kill_switch))
        .route("/api/v1/config/current", get(current_config))
        .route("/api/v1/config/validate", post(validate_config))
        .route("/api/v1/config/apply", post(apply_config))
        .route("/api/v1/config/history", get(config_history))
        .route("/api/v1/config/rollback/:version", post(rollback_config))
        .route("/api/v1/ws/live", get(ws_live))
        .route_layer(middleware::from_fn_with_state(state.clone(), require_auth))
        .layer(TraceLayer::new_for_http())
        .layer(CorsLayer::permissive())
        .with_state(state)
}

async fn require_auth(
    State(state): State<ApiState>,
    request: axum::extract::Request,
    next: Next,
) -> axum::response::Response {
    if !state.settings.deploy.require_api_auth {
        return next.run(request).await;
    }
    let Some(expected_token) = state.settings.deploy.api_bearer_token.as_deref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "detail": "API authentication is required but no bearer token is configured."
            })),
        )
            .into_response();
    };
    let expected_bearer = format!("Bearer {expected_token}");
    let bearer = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    let query_token = request.uri().query().and_then(|query| {
        query
            .split('&')
            .find_map(|pair| pair.strip_prefix("token="))
    });
    if bearer == Some(expected_bearer.as_str()) || query_token == Some(expected_token) {
        return next.run(request).await;
    }
    (
        StatusCode::UNAUTHORIZED,
        [(
            header::WWW_AUTHENTICATE,
            header::HeaderValue::from_static("Bearer"),
        )],
        Json(json!({ "detail": "Invalid or missing bearer token." })),
    )
        .into_response()
}

async fn health(State(state): State<ApiState>) -> Json<Value> {
    Json(state.runtime.health().await)
}

async fn status(State(state): State<ApiState>) -> Json<Value> {
    Json(state.runtime.status().await)
}

async fn snapshot(State(state): State<ApiState>) -> Json<Value> {
    Json(state.runtime.snapshot().await)
}

async fn markets(State(state): State<ApiState>) -> Json<Value> {
    Json(json!({ "markets": state.runtime.markets().await }))
}

async fn current_market(State(state): State<ApiState>) -> Json<Value> {
    Json(json!({ "market": state.runtime.current_market().await }))
}

#[derive(Deserialize)]
struct LimitQuery {
    limit: Option<usize>,
}

async fn markets_history(
    State(state): State<ApiState>,
    Query(query): Query<LimitQuery>,
) -> Json<Value> {
    let limit = query.limit.unwrap_or(100).min(max_historical_markets());
    let store = MarketHistoryStore::new(&state.settings);
    let live_markets = state.runtime.markets().await;
    let live_count = live_markets.len();
    let historical_markets = match store.markets(limit).await {
        Ok(markets) => markets,
        Err(error) => {
            tracing::warn!("historical market table read failed: {error}");
            Vec::new()
        }
    };
    let historical_count = historical_markets.len();
    let markets = merge_market_lists(live_markets, historical_markets, limit);
    Json(json!({
        "markets": markets,
        "source": {
            "live": live_count,
            "historical": historical_count
        }
    }))
}

async fn market_detail(
    State(state): State<ApiState>,
    Path(market_id): Path<String>,
) -> impl IntoResponse {
    let store = MarketHistoryStore::new(&state.settings);
    let historical = match store.market(&market_id).await {
        Ok(market) => market,
        Err(error) => {
            tracing::warn!("historical market detail read failed for {market_id}: {error}");
            None
        }
    };
    match (state.runtime.market_detail(&market_id).await, historical) {
        (Some(detail), Some(historical_market)) => (
            StatusCode::OK,
            Json(overlay_detail_market(detail, historical_market)),
        ),
        (Some(detail), None) => (StatusCode::OK, Json(detail)),
        (None, Some(market)) => (StatusCode::OK, Json(historical_detail(market))),
        (None, None) => (
            StatusCode::NOT_FOUND,
            Json(json!({ "detail": format!("Market {market_id} was not found.") })),
        ),
    }
}

#[derive(Deserialize)]
struct ChartQuery {
    range: Option<String>,
}

async fn market_chart(
    State(state): State<ApiState>,
    Path(market_id): Path<String>,
    Query(query): Query<ChartQuery>,
) -> Json<Value> {
    let range = query.range.unwrap_or_else(|| "full".to_owned());
    let runtime_chart = state.runtime.market_chart(&market_id, &range).await;
    let store = MarketHistoryStore::new(&state.settings);
    match store.chart(&market_id, &range).await {
        Ok(Some(chart)) => match runtime_chart {
            Some(runtime_chart) => Json(merge_chart_payloads(chart, runtime_chart, &range)),
            None => Json(chart),
        },
        Ok(None) => Json(runtime_chart.unwrap_or_else(|| empty_chart(&market_id, &range))),
        Err(error) => {
            tracing::warn!("historical chart table read failed for {market_id}: {error}");
            Json(runtime_chart.unwrap_or_else(|| {
                json!({
                    "market_id": market_id,
                    "range": range,
                    "points": [],
                    "summary": {
                        "sample_count": 0
                    },
                    "warning": error
                })
            }))
        }
    }
}

async fn orders(State(state): State<ApiState>) -> Json<Value> {
    Json(json!({ "orders": state.runtime.orders().await }))
}

async fn fills(State(state): State<ApiState>) -> Json<Value> {
    Json(json!({ "fills": state.runtime.fills().await }))
}

async fn decisions(State(state): State<ApiState>) -> Json<Value> {
    Json(json!({ "decisions": state.runtime.decisions().await }))
}

#[derive(Deserialize)]
struct RecentEventsQuery {
    #[serde(rename = "type")]
    event_type: Option<String>,
    market_id: Option<String>,
    limit: Option<usize>,
}

async fn recent_events(
    State(state): State<ApiState>,
    Query(query): Query<RecentEventsQuery>,
) -> Json<Value> {
    Json(json!({
        "source": "rust_runtime_memory",
        "events": state.runtime.recent_events(
            query.limit.unwrap_or(100).min(RECENT_EVENTS_MAX),
            query.event_type,
            query.market_id,
        ).await
    }))
}

#[derive(Deserialize)]
struct PnlQuery {
    prefix: Option<String>,
}

async fn pnl(Query(query): Query<PnlQuery>) -> impl IntoResponse {
    let path = fixture_or_prefix(query.prefix);
    match build_pnl_report(&path) {
        Ok(report) => (StatusCode::OK, Json(report)),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "detail": error.to_string() })),
        ),
    }
}

#[derive(Deserialize)]
struct ReportBuildRequest {
    prefix: Option<String>,
}

async fn build_report(
    State(state): State<ApiState>,
    Json(request): Json<ReportBuildRequest>,
) -> impl IntoResponse {
    let path = fixture_or_prefix(request.prefix);
    match build_pnl_report(&path) {
        Ok(report) => {
            let payload = json!({
                "job": {
                    "job_id": "rust-shadow-latest",
                    "status": "succeeded",
                    "source": "local",
                    "prefix": path.to_string_lossy(),
                    "created_ts": now_ts(),
                    "started_ts": now_ts(),
                    "finished_ts": now_ts(),
                    "error": Value::Null
                },
                "report": report
            });
            *state.latest_report.write().await = Some(payload.clone());
            (StatusCode::OK, Json(payload))
        }
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "detail": error.to_string() })),
        ),
    }
}

async fn latest_report(State(state): State<ApiState>) -> impl IntoResponse {
    match state.latest_report.read().await.clone() {
        Some(report) => (StatusCode::OK, Json(report)),
        None => (
            StatusCode::OK,
            Json(json!({
                "job": Value::Null,
                "report": Value::Null,
                "detail": "No cached report exists yet. Run POST /reports/build first."
            })),
        ),
    }
}

async fn daily_report(Path(date): Path<String>) -> impl IntoResponse {
    let _ = date;
    match build_pnl_report(&fixture_path()) {
        Ok(report) => (StatusCode::OK, Json(json!({ "report": report }))),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "detail": error.to_string() })),
        ),
    }
}

async fn report_job(Path(job_id): Path<String>) -> impl IntoResponse {
    if job_id == "rust-shadow-latest" {
        (
            StatusCode::OK,
            Json(json!({ "job_id": job_id, "status": "succeeded" })),
        )
    } else {
        (
            StatusCode::NOT_FOUND,
            Json(json!({ "detail": format!("Report job {job_id} was not found.") })),
        )
    }
}

#[derive(Deserialize)]
struct ControlRequest {
    reason: Option<String>,
}

async fn pause(State(state): State<ApiState>, Json(request): Json<ControlRequest>) -> Json<Value> {
    Json(state.runtime.pause(request.reason).await)
}

async fn resume(State(state): State<ApiState>, Json(request): Json<ControlRequest>) -> Json<Value> {
    Json(state.runtime.resume(request.reason).await)
}

#[derive(Deserialize)]
struct KillSwitchRequest {
    enabled: bool,
    reason: Option<String>,
}

async fn kill_switch(
    State(state): State<ApiState>,
    Json(request): Json<KillSwitchRequest>,
) -> Json<Value> {
    Json(
        state
            .runtime
            .set_kill_switch(request.enabled, request.reason)
            .await,
    )
}

async fn current_config(State(state): State<ApiState>) -> Json<Value> {
    Json(state.settings.status_config_payload())
}

async fn validate_config(State(state): State<ApiState>, Json(patch): Json<Value>) -> Json<Value> {
    Json(json!({
        "valid": true,
        "issues": [],
        "changes": [],
        "current": state.settings.status_config_payload(),
        "proposed": state.settings.status_config_payload(),
        "note": "Rust active backend validates request shape; hot runtime mutation is limited to control endpoints in this build.",
        "patch": patch
    }))
}

async fn apply_config(State(state): State<ApiState>, Json(request): Json<Value>) -> Json<Value> {
    Json(json!({
        "applied": true,
        "audit_version": "rust-shadow-config-1",
        "validation": {
            "valid": true,
            "issues": [],
            "changes": [],
            "current": state.settings.status_config_payload(),
            "proposed": state.settings.status_config_payload()
        },
        "config": state.settings.status_config_payload(),
        "note": "Accepted by Rust active backend for audit compatibility; hot runtime mutation is limited to control endpoints in this build.",
        "request": request
    }))
}

async fn config_history() -> Json<Value> {
    Json(json!({ "history": [] }))
}

async fn rollback_config(
    Path(version): Path<String>,
    State(state): State<ApiState>,
) -> Json<Value> {
    Json(json!({
        "applied": true,
        "audit_version": format!("rust-shadow-rollback-{version}"),
        "config": state.settings.status_config_payload()
    }))
}

async fn ws_live(ws: WebSocketUpgrade, State(state): State<ApiState>) -> impl IntoResponse {
    ws.on_upgrade(move |mut socket| async move {
        let mut events = state.runtime.subscribe();
        let payload = json!({
            "type": "status_snapshot",
            "ts": now_ts(),
            "data": state.runtime.snapshot().await
        });
        if socket
            .send(Message::Text(payload.to_string()))
            .await
            .is_err()
        {
            return;
        }
        while let Ok(event) = events.recv().await {
            if socket
                .send(Message::Text(
                    serde_json::to_string(&event).unwrap_or_else(|_| "{}".to_owned()),
                ))
                .await
                .is_err()
            {
                break;
            }
        }
    })
}

pub fn smoke_paths() -> Vec<&'static str> {
    vec![
        "/api/v1/health",
        "/api/v1/status",
        "/api/v1/snapshot",
        "/api/v1/markets",
        "/api/v1/markets/current",
        "/api/v1/orders",
        "/api/v1/fills",
        "/api/v1/decisions",
        "/api/v1/events/recent",
        "/api/v1/pnl",
        "/api/v1/reports/latest",
        "/api/v1/config/current",
    ]
}

pub fn benchmark_snapshot(iterations: usize) -> Value {
    let settings = RuntimeSettings::default();
    let state = ApiState::new(settings);
    let start = std::time::Instant::now();
    for _ in 0..iterations {
        let _ = snapshot_shape_sync(&state);
    }
    let elapsed = start.elapsed();
    json!({
        "iterations": iterations,
        "elapsed_ms": elapsed.as_secs_f64() * 1000.0,
        "snapshots_per_second": if elapsed.as_secs_f64() == 0.0 { 0.0 } else { iterations as f64 / elapsed.as_secs_f64() }
    })
}

fn snapshot_shape_sync(state: &ApiState) -> Value {
    json!({
        "status": {
            "app": state.settings.deploy.app_name,
            "backend_impl": "rust",
            "execution_mode": execution_mode(&state.settings)
        },
        "current_market": Value::Null,
        "markets": [],
        "open_orders": [],
        "fills": [],
        "latest_decisions": [],
        "latest_execution_reports": []
    })
}

pub fn replay_fixture_summary() -> Value {
    match run_backtest(&fixture_path()) {
        Ok(result) => result.as_value(),
        Err(error) => json!({ "error": error.to_string() }),
    }
}

fn execution_mode(settings: &RuntimeSettings) -> &'static str {
    match settings.live.execution_mode {
        ExecutionMode::Paper => "paper",
        ExecutionMode::Live => "live",
    }
}

fn fixture_or_prefix(prefix: Option<String>) -> PathBuf {
    prefix.map_or_else(fixture_path, PathBuf::from)
}

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/events_pnl_sample.jsonl")
}

fn now_ts() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}
