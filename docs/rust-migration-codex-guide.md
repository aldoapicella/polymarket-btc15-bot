# PolyEdge Rust Migration Guide for Codex

Use this document as the detailed guide for a Codex goal. The short Codex goal should refer to this file instead of repeating the whole migration plan.

Recommended repo path:

```text
docs/rust-migration-codex-guide.md
```

---

## Short Codex Goal Prompt

```text
Migrate PolyEdge toward a Rust backend side-by-side with the existing Python backend. Read and follow docs/rust-migration-codex-guide.md exactly.

Do not enable live trading. Do not remove Python. Do not break the Next.js frontend or existing /api/v1 contracts. Build the Rust implementation as a shadow backend with golden-master parity tests, benchmarks, and clear success metrics. Preserve safety gates and paper-mode defaults.

When done, report: implemented crates, API parity status, test results, benchmark results, frontend compatibility, remaining gaps, and next recommendation.
```

---

## 1. Mission

Create a Rust backend for PolyEdge while keeping the Python backend as the reference implementation. The Rust backend must run side-by-side, preserve the existing frontend/API contract, and prove parity before any production cutover.

The migration is successful only if Rust produces the same decisions, fills, reports, and safety blocks as Python on the same data while improving latency, throughput, and reliability.

---

## 2. Non-Negotiable Safety Rules

- Do not enable live trading.
- Do not set `EXECUTION_MODE=live`.
- Do not set `ALLOW_LIVE=true`.
- Do not print, commit, or expose secrets.
- Do not place orders.
- Do not call live order-placement endpoints.
- Do not remove or break the Python backend.
- Do not break the Next.js frontend.
- Rust must default to paper mode.
- Live execution code must be both compile-feature-gated and config-gated.
- Existing safety gates must remain stricter or equal to Python.

---

## 3. Current Architecture to Preserve

PolyEdge currently includes:

- Python package under `src/polyedge/`.
- Modular FastAPI app under `src/polyedge/api/`.
- Next.js frontend under `frontend/`.
- Runtime event bus.
- Snapshot service.
- Chart storage and chart backfill.
- Azure event recording and report storage.
- Runtime paper maker-fill simulation.
- Replay/backtest/PnL reports.
- Scoped live cancellation and heartbeat behind gates.
- Runtime config service and audit log.

The frontend should continue to work against `/api/v1` contracts.

---

## 4. Target Rust Workspace

Create this Rust workspace side-by-side with the Python code:

```text
Cargo.toml
crates/
  polyedge-domain/
  polyedge-config/
  polyedge-feeds/
  polyedge-engine/
  polyedge-execution/
  polyedge-storage/
  polyedge-reporting/
  polyedge-api/
  polyedge-cli/
```

### Crate responsibilities

#### `polyedge-domain`
Pure domain models only. No network, Azure, API, or trading loop.

Include:

```text
MarketSpec
BookState
BookLevel
ReferencePrice
FairValue
TradeDecision
ExecutionReport
RuntimeEvent
RiskAssessment
Outcome
Side
OrderKind
```

Use strong types where practical:

```text
MarketId
ConditionId
TokenId
OrderId
Probability
PriceTicks
ShareSize
UsdPrice
```

#### `polyedge-config`
Load and validate deploy/runtime config.

Separate:

```text
DeployConfig
TargetConfig
StrategyConfig
RiskConfig
PaperConfig
LiveConfig
AzureConfig
```

#### `polyedge-feeds`
Async feed ingestion:

```text
Polymarket RTDS Chainlink
Polymarket RTDS Binance
Polymarket CLOB market websocket
Binance book ticker
Coinbase ticker
```

Each feed emits typed domain events through bounded channels.

#### `polyedge-engine`
Pure core logic:

```text
fair value
EWMA volatility
strategy
risk
order manager
paper fill
settlement
snapshot builder
```

No HTTP, Azure, or live order calls.

#### `polyedge-execution`
Execution adapters:

```text
PaperExecutionClient
LiveClobExecutionClient trait/stub
HeartbeatTask
ScopedCancellation
```

Live adapter must be feature-gated and disabled by default.

#### `polyedge-storage`
Persistence:

```text
JSONL recorder
Azure Blob recorder trait/implementation if practical
Azure Table chart sink trait/implementation if practical
report store
audit log store
runtime config history
```

Local JSONL support is required first. Azure can be staged if needed.

#### `polyedge-reporting`
Replay/backtest/PnL/report generation.

Must match Python fixture outputs.

#### `polyedge-api`
Axum HTTP/WebSocket API matching current `/api/v1` contracts.

#### `polyedge-cli`
CLI parity:

```text
polyedge-rs api
polyedge-rs run
polyedge-rs discover
polyedge-rs confirm-source
polyedge-rs backtest --path ...
polyedge-rs report --prefix ...
polyedge-rs bench-ingest ...
polyedge-rs bench-replay ...
```

---

## 5. Recommended Rust Stack

Use:

```text
tokio
axum
tower-http
serde
serde_json
tracing
tracing-subscriber
thiserror
anyhow only in binaries
clap
reqwest
tokio-tungstenite
rust_decimal or integer ticks
criterion
proptest where useful
```

Production paths should avoid `unwrap()` and `expect()`.

---

## 6. API Contract to Preserve

Rust must implement compatible versions of:

```text
GET  /api/v1/health
GET  /api/v1/status
GET  /api/v1/snapshot
GET  /api/v1/markets
GET  /api/v1/markets/current
GET  /api/v1/markets/{market_id}
GET  /api/v1/markets/{market_id}/chart
GET  /api/v1/orders
GET  /api/v1/fills
GET  /api/v1/decisions
GET  /api/v1/events/recent
GET  /api/v1/pnl
POST /api/v1/reports/build
GET  /api/v1/reports/latest
GET  /api/v1/reports/daily/{date}
GET  /api/v1/reports/{job_id}
POST /api/v1/control/pause
POST /api/v1/control/resume
POST /api/v1/control/kill-switch
GET  /api/v1/config/current
POST /api/v1/config/validate
POST /api/v1/config/apply
WS   /api/v1/ws/live
```

Frontend should require no changes except switching API base URL.

---

## 7. Migration Phases

### Phase 1 — Baseline and fixtures

- Run current Python tests.
- Run frontend typecheck/build.
- Add or export fixtures:
  - decision cases
  - risk cases
  - paper fill cases
  - replay event JSONL
  - PnL/report fixture
- Put fixtures under `tests/fixtures/`.

### Phase 2 — Rust workspace and domain

- Create workspace and crates.
- Port domain types and config.
- Add serde compatibility tests for Python JSON payloads.

### Phase 3 — Pure engine parity

Port:

```text
fair value
volatility
strategy
risk
order manager
paper fill
settlement
```

Add golden-master tests against Python fixtures.

### Phase 4 — Replay/reporting parity

- Port replay/backtest/PnL/report summaries.
- Match Python outputs for fixture files.
- Add market-level statistics and sample-size calculations if already present in Python.

### Phase 5 — API/event bus/snapshot

- Implement Axum API.
- Implement event bus with bounded queues.
- Implement WebSocket `/api/v1/ws/live`.
- Implement snapshot and recent-events endpoints.
- Smoke test with frontend.

### Phase 6 — Runtime loop and feeds

- Implement paper runtime loop.
- Implement feed skeletons and local mock feeds first.
- Add real WebSocket feeds after engine/API parity.
- Add local JSONL recorder.

### Phase 7 — Benchmarks and metrics

Add:

```text
cargo bench
polyedge-rs bench-ingest --events 100000
polyedge-rs bench-replay --path tests/fixtures/events_24h_sample.jsonl
polyedge-rs bench-api-snapshot --iterations 10000
```

### Phase 8 — Shadow deployment

- Add Rust Dockerfile.
- Deploy Rust as shadow backend only.
- Keep production Python backend active.
- Compare Rust and Python reports for the same 24h data.

---

## 8. Golden-Master Parity Requirements

Rust must match Python for:

```text
fair value output
strategy decisions
risk block reasons
order-manager cancel/replace behavior
paper maker fills
replay cancellation behavior
PnL summary
report summary
API snapshot shape
WebSocket event shape
```

Use exact matching for counts and IDs. Use tolerance no greater than `1e-6` for PnL/probability numeric fields.

---

## 9. Performance Success Metrics

Measure Python baseline first, then Rust.

Targets:

```text
Synthetic ingest throughput: >= 5,000 events/sec local
p95 event-to-snapshot latency: <= 50 ms local
p99 event-to-snapshot latency: <= 150 ms local
Replay speed: >= 2x faster than Python on same fixture, or document blocker
Steady-state memory: <= 256 MB paper mode without report job
WebSocket fanout: 10 UI subscribers without stalls
Recorder drops: 0 in benchmark
Open-order leaks after market close: 0
API healthy after container start: < 5 seconds
```

---

## 10. Correctness and Safety Success Metrics

Functional:

```text
Python tests pass
Rust tests pass
Rust API smoke tests pass
Frontend typecheck passes
Frontend build passes
Frontend works against Rust API base URL
Rust replay matches Python fixture output
Rust paper fill output matches Python fixture output
Rust refuses live mode unless all gates pass
```

Safety:

```text
Live disabled by default
No secrets printed
No secrets committed
No live order placement
No account-wide cancel in normal flow
All live functions feature-gated and config-gated
```

Quality:

```text
cargo fmt --check passes
cargo clippy --all-targets --all-features -- -D warnings passes
cargo test --workspace --all-features passes
python -m pytest passes
npm --prefix frontend run typecheck passes
npm --prefix frontend run build passes
```

---

## 11. Observability Requirements

Use structured tracing for:

```text
feed events
reference updates
book updates
fair value updates
decisions
risk blocks
paper fills
cancellations
settlements
report jobs
websocket subscribers
recorder drops/backpressure
```

Expose `/api/v1/status` fields:

```text
backend_impl = rust
git_sha
version
uptime
task health
queue depths
drop counts
feed status
recorder status
event bus subscribers
paper fill stats
heartbeat status, if live feature enabled
```

---

## 12. Engineering Rules

- Core engine must be deterministic.
- Prefer `State + Event -> State + Effects` reducers.
- Use bounded channels everywhere.
- Keep slow report jobs off the hot path.
- Keep API DTOs separate from internal engine types.
- Avoid `unwrap()`/`expect()` in production code.
- Use `thiserror` for library errors.
- Use `anyhow` only in binaries/CLI.
- Keep frontend TypeScript/Next.js unchanged except API base URL if needed.
- Keep Python reference implementation until Rust shadow run proves parity.

---

## 13. Final Report Required From Codex

At completion, report:

```text
What was implemented
Crates created
API endpoints implemented
Fixture parity status
Python test result
Rust fmt result
Rust clippy result
Rust test result
Frontend typecheck result
Frontend build result
API smoke result
Replay parity result
Paper fill parity result
Decision parity result
Risk parity result
Benchmark ingest events/sec
Benchmark p95 latency
Benchmark p99 latency
Benchmark replay time Python
Benchmark replay time Rust
Memory RSS
Live default disabled: yes/no
Secrets printed: no
Remaining gaps
Recommended next step
```

If any required metric cannot be measured, explain why and add a follow-up task.

---

## 14. Cutover Rule

Do not cut over production automatically.

The safe path is:

```text
Rust shadow backend
Replay parity
Frontend compatibility
Synthetic performance proof
24h paper shadow run
Compare Rust vs Python reports
Only then consider paper-runtime cutover
Live remains blocked
```
