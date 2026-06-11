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
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::RwLock;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;

#[derive(Clone)]
pub struct ApiState {
    settings: RuntimeSettings,
    started_at: chrono::DateTime<Utc>,
    paused: Arc<RwLock<bool>>,
    kill_switch: Arc<RwLock<bool>>,
    latest_report: Arc<RwLock<Option<Value>>>,
}

impl ApiState {
    pub fn new(settings: RuntimeSettings) -> Self {
        Self {
            settings,
            started_at: Utc::now(),
            paused: Arc::new(RwLock::new(false)),
            kill_switch: Arc::new(RwLock::new(false)),
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
    Json(json!({
        "ok": true,
        "backend_impl": "rust",
        "execution_mode": execution_mode(&state.settings),
        "kill_switch": *state.kill_switch.read().await,
        "reports": report_status(&state).await
    }))
}

async fn status(State(state): State<ApiState>) -> Json<Value> {
    Json(status_payload(&state).await)
}

async fn snapshot(State(state): State<ApiState>) -> Json<Value> {
    Json(snapshot_payload(&state).await)
}

async fn markets() -> Json<Value> {
    Json(json!({ "markets": [] }))
}

async fn current_market() -> Json<Value> {
    Json(json!({ "market": Value::Null }))
}

#[derive(Deserialize)]
struct LimitQuery {
    limit: Option<usize>,
}

async fn markets_history(Query(query): Query<LimitQuery>) -> Json<Value> {
    let _limit = query.limit.unwrap_or(100);
    Json(json!({ "markets": [] }))
}

async fn market_detail(Path(market_id): Path<String>) -> impl IntoResponse {
    (
        StatusCode::NOT_FOUND,
        Json(json!({ "detail": format!("Market {market_id} was not found.") })),
    )
}

#[derive(Deserialize)]
struct ChartQuery {
    range: Option<String>,
}

async fn market_chart(
    Path(market_id): Path<String>,
    Query(query): Query<ChartQuery>,
) -> Json<Value> {
    Json(json!({
        "market_id": market_id,
        "range": query.range.unwrap_or_else(|| "full".to_owned()),
        "points": [],
        "summary": {
            "sample_count": 0
        }
    }))
}

async fn orders() -> Json<Value> {
    Json(json!({ "orders": [] }))
}

async fn fills() -> Json<Value> {
    Json(json!({ "fills": [] }))
}

async fn decisions() -> Json<Value> {
    Json(json!({ "decisions": [] }))
}

#[derive(Deserialize)]
struct RecentEventsQuery {
    #[serde(rename = "type")]
    event_type: Option<String>,
    market_id: Option<String>,
    limit: Option<usize>,
}

async fn recent_events(Query(query): Query<RecentEventsQuery>) -> Json<Value> {
    let _ = (
        query.event_type,
        query.market_id,
        query.limit.unwrap_or(100),
    );
    Json(json!({ "source": "rust_shadow_memory", "events": [] }))
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
            StatusCode::NOT_FOUND,
            Json(
                json!({ "detail": "No cached report exists yet. Run POST /reports/build first." }),
            ),
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
    *state.paused.write().await = true;
    Json(json!({
        "control": {
            "paused": true,
            "paused_at": now_ts(),
            "pause_reason": request.reason
        },
        "audit_version": "rust-shadow-control-1"
    }))
}

async fn resume(State(state): State<ApiState>, Json(request): Json<ControlRequest>) -> Json<Value> {
    let _ = request.reason;
    *state.paused.write().await = false;
    Json(json!({
        "control": {
            "paused": false,
            "paused_at": Value::Null,
            "pause_reason": Value::Null
        },
        "audit_version": "rust-shadow-control-2"
    }))
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
    let _ = request.reason;
    *state.kill_switch.write().await = request.enabled;
    Json(json!({ "enabled": request.enabled, "audit_version": "rust-shadow-kill-switch-1" }))
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
        "shadow_note": "Rust shadow backend validates request shape but does not mutate Python runtime config.",
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
        "shadow_note": "Accepted by Rust shadow backend only; Python runtime remains authoritative.",
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
        let payload = json!({
            "type": "status_snapshot",
            "ts": now_ts(),
            "data": snapshot_payload(&state).await
        });
        let _send_result = socket.send(Message::Text(payload.to_string())).await;
    })
}

async fn report_status(state: &ApiState) -> Value {
    json!({
        "running_job": Value::Null,
        "known_jobs": usize::from(state.latest_report.read().await.is_some()),
        "store": {
            "backend_impl": "rust",
            "shadow_only": true
        }
    })
}

async fn status_payload(state: &ApiState) -> Value {
    let now = Utc::now();
    json!({
        "app": state.settings.deploy.app_name,
        "backend_impl": "rust",
        "git_sha": option_env!("GIT_SHA").unwrap_or("unknown"),
        "version": env!("CARGO_PKG_VERSION"),
        "execution_mode": execution_mode(&state.settings),
        "started_at": state.started_at.to_rfc3339_opts(SecondsFormat::Secs, true),
        "now": now.to_rfc3339_opts(SecondsFormat::Secs, true),
        "uptime": now.signed_duration_since(state.started_at).num_seconds(),
        "markets": 0,
        "tradeable_markets": 0,
        "books": 0,
        "tracked_open_orders": 0,
        "control": {
            "paused": *state.paused.read().await
        },
        "kill_switch": *state.kill_switch.read().await,
        "task_health": {
            "api": "ok",
            "runtime_loop": "shadow_idle",
            "feeds": "mock_or_disabled"
        },
        "queue_depths": {
            "feed_events": 0,
            "runtime_events": 0,
            "recorder": 0
        },
        "drop_counts": {
            "feed_events": 0,
            "runtime_events": 0,
            "recorder": 0
        },
        "feed_status": {
            "polymarket_rtds_chainlink": "not_started",
            "polymarket_rtds_binance": "not_started",
            "polymarket_clob_market": "not_started",
            "binance_book_ticker": "not_started",
            "coinbase_ticker": "not_started"
        },
        "recorder_status": {
            "backend": "local_jsonl",
            "drops": 0
        },
        "event_bus_subscribers": 0,
        "paper_fill": {
            "paper_fill_policy": state.settings.paper.maker_fill_policy,
            "paper_order_live_after_ms": state.settings.paper.order_live_after_ms,
            "paper_open_resting_orders": 0,
            "paper_maker_fills": 0
        },
        "paper_fill_stats": {
            "maker_fills": 0,
            "prevented_not_live": 0,
            "prevented_stale_book": 0,
            "prevented_final_window": 0,
            "prevented_market_inactive": 0,
            "prevented_expired": 0,
            "prevented_after_cancel": 0
        },
        "heartbeat_status": {
            "enabled": state.settings.live.enable_heartbeat,
            "status": "disabled_in_shadow"
        },
        "live_heartbeat": Value::Null,
        "recorder": {
            "backend": "local_jsonl",
            "drops": 0
        },
        "reference": Value::Null,
        "reports": report_status(state).await,
        "latest_decisions": [],
        "latest_execution_reports": []
    })
}

async fn snapshot_payload(state: &ApiState) -> Value {
    json!({
        "status": status_payload(state).await,
        "current_market": Value::Null,
        "markets": [],
        "open_orders": [],
        "fills": [],
        "latest_decisions": [],
        "latest_execution_reports": []
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
