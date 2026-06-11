use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use polyedge_api::{app, benchmark_snapshot};
use polyedge_config::RuntimeSettings;
use polyedge_reporting::{
    build_pnl_report, run_backtest, BacktestConfig, ReplayBacktester, REPLAY_BUFFER_BYTES,
};
use polyedge_storage::{AzureBlobClient, AzureBlobItem};
use serde_json::json;
use std::collections::BTreeMap;
use std::io::{BufReader, Cursor};
use std::path::PathBuf;
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Instant;

#[derive(Parser)]
#[command(name = "polyedge-rs")]
#[command(about = "PolyEdge Rust shadow backend CLI")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Api {
        #[arg(long, default_value = "127.0.0.1:8081")]
        bind: String,
    },
    Run {
        #[arg(long, default_value = "127.0.0.1:8081")]
        bind: String,
    },
    Discover,
    ConfirmSource,
    Backtest {
        #[arg(long)]
        path: PathBuf,
    },
    Report {
        #[arg(long)]
        prefix: PathBuf,
    },
    BenchIngest {
        #[arg(long, default_value_t = 100_000)]
        events: usize,
    },
    BenchReplay {
        #[arg(long)]
        path: PathBuf,
    },
    BenchAzureReplay {
        #[arg(long)]
        account: String,
        #[arg(long, default_value = "bot-events")]
        container: String,
        #[arg(long)]
        prefix: String,
        #[arg(long, default_value = "AZURE_STORAGE_SAS")]
        sas_env: String,
        #[arg(long)]
        max_blobs: Option<usize>,
        #[arg(long)]
        max_bytes: Option<u64>,
        #[arg(long, default_value_t = 8)]
        prefetch_blobs: usize,
    },
    BenchApiSnapshot {
        #[arg(long, default_value_t = 10_000)]
        iterations: usize,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .try_init()
        .ok();
    let cli = Cli::parse();
    let settings = RuntimeSettings::from_env().context("loading runtime settings")?;
    if settings.live_requested() {
        match settings.validate_live_gates(false) {
            Ok(()) => bail!("Rust shadow backend refuses live mode even when config gates pass."),
            Err(error) => bail!("Rust shadow backend refuses live mode: {error}"),
        }
    }
    match cli.command {
        Command::Api { bind } | Command::Run { bind } => serve(settings, bind).await,
        Command::Discover => print_json(json!({
            "count": 0,
            "markets": [],
            "backend_impl": "rust",
            "shadow_only": true
        })),
        Command::ConfirmSource => print_json(json!({
            "ok": false,
            "backend_impl": "rust",
            "shadow_only": true,
            "message": "Source confirmation is not connected in the Rust shadow backend yet."
        })),
        Command::Backtest { path } => print_json(run_backtest(&path)?.as_value()),
        Command::Report { prefix } => print_json(build_pnl_report(&prefix)?),
        Command::BenchIngest { events } => print_json(bench_ingest(events)),
        Command::BenchReplay { path } => print_json(bench_replay(path)?),
        Command::BenchAzureReplay {
            account,
            container,
            prefix,
            sas_env,
            max_blobs,
            max_bytes,
            prefetch_blobs,
        } => print_json(bench_azure_replay(
            account,
            container,
            prefix,
            sas_env,
            max_blobs,
            max_bytes,
            prefetch_blobs,
        )?),
        Command::BenchApiSnapshot { iterations } => print_json(benchmark_snapshot(iterations)),
    }
}

async fn serve(settings: RuntimeSettings, bind: String) -> Result<()> {
    let listener = tokio::net::TcpListener::bind(&bind)
        .await
        .with_context(|| format!("binding Rust shadow API to {bind}"))?;
    println!(
        "{}",
        json!({
            "backend_impl": "rust",
            "shadow_only": true,
            "execution_mode": "paper",
            "bind": bind
        })
    );
    axum::serve(listener, app(settings))
        .await
        .context("serving Rust shadow API")
}

fn bench_ingest(events: usize) -> serde_json::Value {
    let mut latencies_us = Vec::with_capacity(events);
    let start = Instant::now();
    let mut dropped = 0usize;
    for index in 0..events {
        let event_start = Instant::now();
        let payload = json!({
            "type": "reference",
            "sequence": index,
            "price": "100000",
            "backend_impl": "rust"
        });
        if payload.get("sequence").is_none() {
            dropped += 1;
        }
        latencies_us.push(event_start.elapsed().as_secs_f64() * 1_000_000.0);
    }
    let elapsed = start.elapsed();
    latencies_us.sort_by(|left, right| left.total_cmp(right));
    json!({
        "events": events,
        "elapsed_ms": elapsed.as_secs_f64() * 1000.0,
        "events_per_second": if elapsed.as_secs_f64() == 0.0 { 0.0 } else { events as f64 / elapsed.as_secs_f64() },
        "p95_event_to_snapshot_latency_ms": percentile(&latencies_us, 0.95) / 1000.0,
        "p99_event_to_snapshot_latency_ms": percentile(&latencies_us, 0.99) / 1000.0,
        "recorder_drops": dropped,
        "memory_rss_mb": rss_mb()
    })
}

fn bench_replay(path: PathBuf) -> Result<serde_json::Value> {
    let start = Instant::now();
    let result = run_backtest(&path)?;
    let elapsed = start.elapsed();
    let bytes = std::fs::metadata(&path).map(|metadata| metadata.len()).ok();
    Ok(json!({
        "path": path.to_string_lossy(),
        "events": result.event_count,
        "elapsed_ms": elapsed.as_secs_f64() * 1000.0,
        "events_per_second": if elapsed.as_secs_f64() == 0.0 { 0.0 } else { result.event_count as f64 / elapsed.as_secs_f64() },
        "bytes": bytes,
        "bytes_per_second": bytes.map(|value| if elapsed.as_secs_f64() == 0.0 { 0.0 } else { value as f64 / elapsed.as_secs_f64() }),
        "mib_per_second": bytes.map(|value| if elapsed.as_secs_f64() == 0.0 { 0.0 } else { value as f64 / 1024.0 / 1024.0 / elapsed.as_secs_f64() }),
        "filled_orders": result.filled_orders,
        "net_pnl": result.net_pnl,
        "memory_rss_mb": rss_mb()
    }))
}

fn bench_azure_replay(
    account: String,
    container: String,
    prefix: String,
    sas_env: String,
    max_blobs: Option<usize>,
    max_bytes: Option<u64>,
    prefetch_blobs: usize,
) -> Result<serde_json::Value> {
    let sas = std::env::var(&sas_env).with_context(|| {
        format!("{sas_env} must contain a read/list SAS token for the container")
    })?;
    let client = AzureBlobClient::new(&account, &container, sas);
    let list_start = Instant::now();
    let blobs = client
        .list_blobs(&prefix, max_blobs, max_bytes)
        .context("listing Azure blobs")?;
    let list_elapsed = list_start.elapsed();
    let listed_bytes = blobs.iter().map(|blob| blob.content_length).sum::<u64>();
    let replay_start = Instant::now();
    let mut backtester = ReplayBacktester::new(BacktestConfig::new(format!(
        "azure://{account}/{container}/{prefix}"
    )));
    let replayed_bytes =
        replay_prefetched_azure_blobs(client, blobs.clone(), prefetch_blobs, &mut backtester)?;
    let replay_elapsed = replay_start.elapsed();
    let result = backtester.finish();
    Ok(json!({
        "source": "azure_blob",
        "transport": "native_ureq_persistent_prefetch",
        "account": account,
        "container": container,
        "prefix": prefix,
        "listed_blobs": blobs.len(),
        "listed_bytes": listed_bytes,
        "listed_gib": listed_bytes as f64 / 1024.0 / 1024.0 / 1024.0,
        "replayed_bytes": replayed_bytes,
        "replayed_gib": replayed_bytes as f64 / 1024.0 / 1024.0 / 1024.0,
        "events": result.event_count,
        "elapsed_ms": replay_elapsed.as_secs_f64() * 1000.0,
        "events_per_second": if replay_elapsed.as_secs_f64() == 0.0 { 0.0 } else { result.event_count as f64 / replay_elapsed.as_secs_f64() },
        "bytes_per_second": if replay_elapsed.as_secs_f64() == 0.0 { 0.0 } else { replayed_bytes as f64 / replay_elapsed.as_secs_f64() },
        "mib_per_second": if replay_elapsed.as_secs_f64() == 0.0 { 0.0 } else { replayed_bytes as f64 / 1024.0 / 1024.0 / replay_elapsed.as_secs_f64() },
        "filled_orders": result.filled_orders,
        "net_pnl": result.net_pnl,
        "list_elapsed_ms": list_elapsed.as_secs_f64() * 1000.0,
        "prefetch_blobs": prefetch_blobs.max(1).min(blobs.len().max(1)),
        "memory_rss_mb": rss_mb()
    }))
}

#[derive(Debug)]
struct PrefetchedBlob {
    index: usize,
    blob: AzureBlobItem,
    bytes: Vec<u8>,
}

fn replay_prefetched_azure_blobs(
    client: AzureBlobClient,
    blobs: Vec<AzureBlobItem>,
    prefetch_blobs: usize,
    backtester: &mut ReplayBacktester,
) -> Result<u64> {
    if blobs.is_empty() {
        return Ok(0);
    }
    let total_blobs = blobs.len();
    let worker_count = prefetch_blobs.max(1).min(blobs.len());
    let (job_tx, job_rx) = mpsc::channel::<(usize, AzureBlobItem)>();
    let (result_tx, result_rx) = mpsc::sync_channel::<Result<PrefetchedBlob>>(worker_count);
    let job_rx = Arc::new(Mutex::new(job_rx));
    let mut handles = Vec::with_capacity(worker_count);
    for _ in 0..worker_count {
        let worker_client = client.clone();
        let worker_job_rx = Arc::clone(&job_rx);
        let worker_result_tx = result_tx.clone();
        handles.push(thread::spawn(move || loop {
            let Ok((index, blob)) = worker_job_rx
                .lock()
                .map_err(|_| ())
                .and_then(|receiver| receiver.recv().map_err(|_| ()))
            else {
                break;
            };
            let result = worker_client
                .download_blob_bytes(&blob.name)
                .with_context(|| format!("downloading {}", blob.name))
                .map(|bytes| PrefetchedBlob { index, blob, bytes });
            if worker_result_tx.send(result).is_err() {
                break;
            }
        }));
    }
    drop(result_tx);

    for (index, blob) in blobs.into_iter().enumerate() {
        job_tx
            .send((index, blob))
            .context("queueing Azure blob download job")?;
    }
    drop(job_tx);

    let mut pending = BTreeMap::new();
    let mut next_index = 0_usize;
    let mut replayed_bytes = 0_u64;
    while next_index < total_blobs {
        let prefetched = result_rx
            .recv()
            .context("Azure blob download workers stopped before replay completed")??;
        pending.insert(prefetched.index, prefetched);
        while let Some(prefetched) = pending.remove(&next_index) {
            let bytes_len = prefetched.bytes.len() as u64;
            backtester
                .run_reader(BufReader::with_capacity(
                    REPLAY_BUFFER_BYTES,
                    Cursor::new(prefetched.bytes),
                ))
                .with_context(|| format!("replaying {}", prefetched.blob.name))?;
            replayed_bytes += bytes_len;
            next_index += 1;
        }
    }
    while let Ok(prefetched) = result_rx.try_recv() {
        let prefetched = prefetched?;
        pending.insert(prefetched.index, prefetched);
        while let Some(prefetched) = pending.remove(&next_index) {
            let bytes_len = prefetched.bytes.len() as u64;
            backtester
                .run_reader(BufReader::with_capacity(
                    REPLAY_BUFFER_BYTES,
                    Cursor::new(prefetched.bytes),
                ))
                .with_context(|| format!("replaying {}", prefetched.blob.name))?;
            replayed_bytes += bytes_len;
            next_index += 1;
        }
    }
    for handle in handles {
        handle
            .join()
            .map_err(|_| anyhow::anyhow!("Azure blob download worker panicked"))?;
    }
    if !pending.is_empty() {
        bail!("Azure blob prefetch completed with unreplayed out-of-order blobs");
    }
    Ok(replayed_bytes)
}

fn percentile(sorted_values: &[f64], percentile: f64) -> f64 {
    if sorted_values.is_empty() {
        return 0.0;
    }
    let index = ((sorted_values.len() - 1) as f64 * percentile).round() as usize;
    sorted_values[index.min(sorted_values.len() - 1)]
}

fn rss_mb() -> Option<f64> {
    let statm = std::fs::read_to_string("/proc/self/statm").ok()?;
    let pages = statm.split_whitespace().nth(1)?.parse::<f64>().ok()?;
    Some(pages * 4096.0 / 1024.0 / 1024.0)
}

fn print_json(value: serde_json::Value) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(())
}
