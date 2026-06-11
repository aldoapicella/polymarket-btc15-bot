# PolyEdge Rust Shadow Backend Status

## Success Metrics

Rust shadow backend remains side-by-side with Python. Python is still the reference implementation and the Next.js frontend keeps using the existing `/api/v1` contract through its server-side proxy.

Safety state:

- `EXECUTION_MODE` defaults to `paper`.
- `ALLOW_LIVE` defaults to `false`.
- Live execution is behind the `polyedge-execution/live` compile feature and still fails config validation unless every gate passes.
- Rust live order placement and live cancellation are stubbed and return blocked errors.
- Rust API bearer auth now matches the Python deployment contract when `REQUIRE_API_AUTH=true`.
- No Python backend files were removed.

Implemented crates:

- `polyedge-domain`
- `polyedge-config`
- `polyedge-feeds`
- `polyedge-engine`
- `polyedge-execution`
- `polyedge-storage`
- `polyedge-reporting`
- `polyedge-api`
- `polyedge-cli`

Parity fixtures:

- `tests/fixtures/rust_parity_cases.json`
- `tests/fixtures/events_pnl_sample.jsonl`
- `tests/fixtures/events_cancelled_maker_sample.jsonl`
- `tests/fixtures/pnl_report_expected.json`
- `tests/fixtures/backtest_cancelled_maker_expected.json`

Verification commands:

```bash
TMPDIR=/tmp .venv/bin/python -m pytest
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
cargo run -p polyedge-cli -- bench-ingest --events 100000
cargo run -p polyedge-cli -- bench-replay --path tests/fixtures/events_pnl_sample.jsonl
cargo run -p polyedge-cli -- bench-api-snapshot --iterations 10000
```

Latest local results:

- Python tests: 85 passed, 1 upstream FastAPI/TestClient deprecation warning.
- Rust fmt: passed.
- Rust tests: passed, including API smoke/auth/contract coverage and golden-master engine/reporting/paper-fill parity.
- Rust clippy: passed with `-D warnings`.
- Frontend typecheck: no frontend files were changed by the Rust migration; a current rerun is blocked in this shell because `npm` is not on PATH and bundled Windows `node.exe` fails through the WSL vsock bridge.
- Frontend build: blocked in this WSL workspace because only Windows Node is available through the bundled runtime while installed Next SWC is Linux-targeted; a later retry hit the WSL vsock bridge error.
- Rust HTTP smoke: `/api/v1/health`, `/api/v1/status`, `/api/v1/snapshot`, and `/api/v1/pnl` returned 200 from `127.0.0.1:18081`.
- Synthetic ingest benchmark: 100,000 events in 131.16 ms, 762,423.72 events/sec, p95 0.001219 ms, p99 0.001657 ms, 0 drops, RSS 8.29 MB.
- API snapshot benchmark: 10,000 snapshots in 32.07 ms, 311,801.18 snapshots/sec.
- Python replay fixture benchmark: 5 events in 16.98 ms, 294.47 events/sec.
- Rust replay fixture benchmark: 5 events in 31.51 ms, 158.67 events/sec, RSS 7.81 MB.
- Criterion fair value benchmark: 147.76 ns per call.
- Criterion maker strategy benchmark: 414.21 ns per call.
- Live-mode refusal: verified nonzero exit with `ALLOW_LIVE is false`, missing location confirmation, missing private key, and missing exact Chainlink source.

Shadow deployment evidence:

- Built and pushed ACR image: `crpolyedge6urdjr5nmwx7w.azurecr.io/polyedge-rust-shadow:rust-shadow-20260611T084002Z`.
- ACR run `ca1` succeeded and pushed digest `sha256:96fb2c17de30f1670e81e8bb9b1f4dd49dd97fa7758afc99d75e71ea3d7b3025`.
- Deployed separate Container App: `polyedge-rust-shadow`.
- Shadow FQDN: `https://polyedge-rust-shadow.graypond-7f5d8417.eastus.azurecontainerapps.io/`.
- Shadow revision: `polyedge-rust-shadow--x44gcth`.
- Shadow safety env: `EXECUTION_MODE=paper`, `ALLOW_LIVE=false`, `RUN_BOT_ON_STARTUP=false`, `REQUIRE_API_AUTH=true`, `API_BEARER_TOKEN` from secret `api-bearer-token`.
- Shadow auth check: unauthenticated `/api/v1/health` returned 401; authenticated `/api/v1/health` and `/api/v1/status` returned 200 with `backend_impl=rust` and `execution_mode=paper`.
- Active Python app was not cut over and still returned 200 from `https://polyedge-dev.graypond-7f5d8417.eastus.azurecontainerapps.io/api/backend/health`.

Azure production-data replay evidence:

- Account/container verified: `stpolyedge6urdjr5nmwx7w` / `bot-events`.
- Event prefix verified: `events/`.
- JSONL blobs listed in the latest full replay: 12,366.
- Total listed JSONL data in the latest full replay: 130,864,400,322 bytes / 121.877 GiB.
- Total replayed bytes in the latest full replay: 130,868,042,975 bytes / 121.880 GiB. The replayed byte count is slightly higher than the list snapshot because active append blobs can grow between list and download.
- Covered modified range from the earlier inventory: 2026-06-06T03:40:50Z through 2026-06-11T05:39:56Z.
- Largest complete day observed: 2026-06-10 with 1,440 minute blobs and 15.406 GiB.

Azure benchmark commands use short-lived process-local credentials only. Keys and SAS tokens are not printed.

Real-data benchmark results:

- Rust local replay over `output/bench/azure-20260610-0000.jsonl`: 22,481 events in 231.88 ms, 96,950 events/sec, 41.74 MiB/sec, RSS 4.40 MB.
- Python local replay over the same 9.7 MiB blob: 22,481 events in 1,310.74 ms, 17,151 events/sec, 7.38 MiB/sec, RSS 12.97 MB.
- Previous Rust curl Azure-stream replay over `events/2026/06/10/`, capped at 134,217,728 bytes: 13 blobs, 122,755,898 bytes, 275,940 events in 47.64 s, 5,792 events/sec, 2.46 MiB/sec, RSS 4.41 MB.
- Rust native persistent/prefetch Azure replay over the same 134,217,728-byte cap: 13 blobs, 122,755,898 bytes, 275,940 events in 3.95 s, 69,879 events/sec, 29.65 MiB/sec, RSS 48.74 MB, `prefetch_blobs=4`.
- Rust native persistent/prefetch Azure replay over `events/2026/06/10/`, capped at 1 GiB: 101 blobs, 1,072,242,422 bytes, 2,415,891 events in 14.41 s, 167,709 events/sec, 70.99 MiB/sec, RSS 62.00 MB, `prefetch_blobs=8`.
- Rust native persistent/prefetch Azure replay over full `events/`: 12,366 blobs, 130,868,042,975 replayed bytes / 121.880 GiB, 293,973,783 events in 2,213.40 s, 132,816 events/sec, 56.39 MiB/sec, RSS 525.31 MB, `prefetch_blobs=8`.
- Python Azure-stream replay over the same prefix and cap: 13 blobs, 122,755,898 bytes, 275,940 events in 9.82 s, 28,091 events/sec, 11.92 MiB/sec, RSS 105.79 MB.
- Raw `curl` against the same one-blob SAS path downloaded 10,149,200 bytes in 2.07 s at 4.67 MiB/sec.

Benchmark conclusion:

- The Rust replay core is faster than Python once data is local: about 5.65x higher event throughput and much lower RSS on the sampled production blob.
- The temporary Rust `curl` Azure path has been replaced with a native Rust Azure Blob REST client backed by a persistent `ureq::Agent`, bounded parallel blob prefetch, and ordered streaming into `ReplayBacktester`.
- The native Rust Azure path is now faster than the Python Azure SDK sample on the same 134 MiB cap: 3.95 s versus 9.82 s, with lower RSS.
- The full current `events/` prefix replay completed successfully against 121.880 GiB of downloaded JSONL data.
- Next performance step: move from whole-blob byte prefetch to chunk-level streaming prefetch for very large blobs, and add progress logging for long full-prefix runs.

Known staged gaps:

- Real WebSocket feed connectors are typed skeletons only.
- Azure Blob recording and Azure Table chart/report persistence are still staged; Azure replay/list/download is implemented through the native Rust Blob REST client.
- API runtime state is shadow/empty by default until the Rust runtime loop is connected. This blocks production cutover.
- A 24h Rust shadow runtime comparison against the active Python paper bot has not been completed yet. This blocks production cutover under the migration guide's cutover rule.
- Frontend build needs a Linux Node runtime or Windows SWC package alignment in this WSL workspace.
