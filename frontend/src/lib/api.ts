"use client";

import type {
  ConfigAuditEntry,
  ConfigValidation,
  JsonRecord,
  MarketDetail,
  ReportJob,
  ReportPayload,
  RuntimeEvent,
  RuntimeConfig,
  RuntimeConfigPatch,
  Snapshot
} from "@/lib/types";
import type { ChartPoint, ChartRange, MarketSeries } from "@/lib/charting";

type FetchOptions = {
  method?: "GET" | "POST";
  body?: unknown;
};

export async function backendFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const response = await fetch(`/api/backend/${path.replace(/^\//, "")}`, {
    method: options.method ?? "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
    cache: "no-store"
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = payload?.detail ?? payload?.error ?? response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload as T;
}

export function getSnapshot() {
  return backendFetch<Snapshot>("snapshot");
}

export function getMarketDetail(marketId: string) {
  return backendFetch<MarketDetail>(`markets/${encodeURIComponent(marketId)}`);
}

export function getHistoricalMarkets(limit = 200) {
  return backendFetch<{ markets: Snapshot["markets"] }>(`markets/history?limit=${limit}`);
}

export function getMarketChart(marketId: string, range: ChartRange = "full") {
  const query = new URLSearchParams({ range });
  return backendFetch<unknown>(`markets/${encodeURIComponent(marketId)}/chart?${query.toString()}`).then((payload) =>
    normalizeMarketSeries(payload, range)
  );
}

export function getRecentEvents(params: { marketId?: string; type?: string; limit?: number } = {}) {
  const query = new URLSearchParams();
  if (params.marketId) {
    query.set("market_id", params.marketId);
  }
  if (params.type) {
    query.set("type", params.type);
  }
  if (params.limit) {
    query.set("limit", String(params.limit));
  }
  return backendFetch<{ source?: string; warning?: string; events: RuntimeEvent[] }>(
    `events/recent${query.size ? `?${query.toString()}` : ""}`
  );
}

export function getLatestReport() {
  return backendFetch<ReportPayload>("reports/latest");
}

export function getDailyReport(date: string) {
  return backendFetch<ReportPayload>(`reports/daily/${date}`);
}

export async function buildReport(body: {
  source: "auto" | "local" | "azure";
  prefix?: string | null;
  date?: string | null;
  force?: boolean;
  settlement_window_seconds?: number;
}) {
  const payload = await backendFetch<ReportPayload | ReportJob>("reports/build", {
    method: "POST",
    body
  });
  if ("job_id" in payload) {
    return { job: payload, report: null } satisfies ReportPayload;
  }
  return payload;
}

export function getConfig() {
  return backendFetch<RuntimeConfig>("config/current");
}

export function validateConfig(config: RuntimeConfigPatch) {
  return backendFetch<ConfigValidation>("config/validate", {
    method: "POST",
    body: config
  });
}

export function applyConfig(config: RuntimeConfigPatch, reason: string) {
  return backendFetch<{ applied: boolean; audit_version: string; validation: ConfigValidation; config: RuntimeConfig }>(
    "config/apply",
    {
      method: "POST",
      body: {
        config,
        reason,
        source: "ui"
      }
    }
  );
}

export function getConfigHistory(limit = 20) {
  return backendFetch<{ history: ConfigAuditEntry[] }>(`config/history?limit=${limit}`);
}

export function rollbackConfig(version: string, reason: string) {
  const query = new URLSearchParams({ reason, source: "ui" });
  return backendFetch<{ applied: boolean; audit_version: string; config: RuntimeConfig }>(
    `config/rollback/${encodeURIComponent(version)}?${query.toString()}`,
    { method: "POST" }
  );
}

export function setKillSwitch(enabled: boolean, reason: string) {
  return backendFetch<{ enabled: boolean; audit_version: string }>("control/kill-switch", {
    method: "POST",
    body: {
      enabled,
      reason,
      source: "ui"
    }
  });
}

export function pauseBot(reason: string) {
  return backendFetch<{ control: { paused: boolean }; audit_version: string }>("control/pause", {
    method: "POST",
    body: {
      reason,
      source: "ui"
    }
  });
}

export function resumeBot(reason: string) {
  return backendFetch<{ control: { paused: boolean }; audit_version: string }>("control/resume", {
    method: "POST",
    body: {
      reason,
      source: "ui"
    }
  });
}

function normalizeMarketSeries(payload: unknown, requestedRange: ChartRange): MarketSeries {
  const record = asRecord(payload) ?? {};
  const marketChart = chartPoints(record.marketChart ?? record.points);
  const explicitFills = chartPoints(record.fills);
  const fills = explicitFills.length ? explicitFills : marketChart.filter((point) => point.fillPrice !== undefined);
  const sampleCount =
    numeric(record.sampleCount) ??
    numeric(asRecord(record.summary)?.sample_count) ??
    numeric(asRecord(record.summary)?.sampleCount) ??
    marketChart.length;

  return {
    source: text(record.source),
    warning: text(record.warning) ?? null,
    market_id: text(record.market_id) ?? text(record.marketId),
    range: chartRange(record.range) ?? requestedRange,
    marketChart,
    fills,
    domain: domain(record.domain) ?? derivedDomain([...marketChart, ...fills]) ?? defaultDomain(),
    sampleCount
  };
}

function chartPoints(value: unknown): ChartPoint[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(chartPoint).filter((point): point is ChartPoint => point !== null);
}

function chartPoint(value: unknown): ChartPoint | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  const bucket =
    numeric(record.bucket) ??
    timestamp(record.time) ??
    timestamp(record.ts) ??
    timestamp(record.local_ts) ??
    timestamp(record.recorded_ts);
  if (bucket === undefined) {
    return null;
  }
  const point: ChartPoint = {
    bucket,
    time: text(record.time) ?? text(record.ts) ?? text(record.local_ts) ?? new Date(bucket).toISOString()
  };
  assignNumber(point, "qUp", record.qUp, record.q_up);
  assignNumber(point, "qDown", record.qDown, record.q_down);
  assignNumber(point, "upBid", record.upBid, record.up_bid);
  assignNumber(point, "upAsk", record.upAsk, record.up_ask);
  assignNumber(point, "downBid", record.downBid, record.down_bid);
  assignNumber(point, "downAsk", record.downAsk, record.down_ask);
  assignNumber(point, "distanceBps", record.distanceBps, record.distance_bps);
  assignNumber(point, "referencePrice", record.referencePrice, record.reference_price);
  assignNumber(point, "fillPrice", record.fillPrice, record.fill_price);
  assignNumber(point, "fillSize", record.fillSize, record.fill_size);
  point.fillOutcome = text(record.fillOutcome) ?? text(record.fill_outcome);
  return point;
}

function assignNumber(point: ChartPoint, key: keyof ChartPoint, ...values: unknown[]) {
  const value = values.map(numeric).find((candidate) => candidate !== undefined);
  if (value !== undefined) {
    (point as Record<keyof ChartPoint, unknown>)[key] = value;
  }
}

function domain(value: unknown): [number, number] | undefined {
  if (!Array.isArray(value) || value.length !== 2) {
    return undefined;
  }
  const start = numeric(value[0]);
  const end = numeric(value[1]);
  return start !== undefined && end !== undefined && end > start ? [start, end] : undefined;
}

function derivedDomain(points: ChartPoint[]): [number, number] | undefined {
  if (!points.length) {
    return undefined;
  }
  const buckets = points.map((point) => point.bucket).filter(Number.isFinite);
  if (!buckets.length) {
    return undefined;
  }
  const start = Math.min(...buckets);
  const end = Math.max(...buckets);
  return end > start ? [start, end] : [start, start + 15 * 60 * 1000];
}

function defaultDomain(): [number, number] {
  const now = Date.now();
  return [now - 15 * 60 * 1000, now];
}

function chartRange(value: unknown): ChartRange | undefined {
  return value === "full" || value === "5m" || value === "1m" ? value : undefined;
}

function timestamp(value: unknown): number | undefined {
  const raw = text(value);
  if (!raw) {
    return undefined;
  }
  const parsed = new Date(raw).getTime();
  return Number.isFinite(parsed) ? parsed : undefined;
}

function numeric(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.length ? value : undefined;
}

function asRecord(value: unknown): JsonRecord | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonRecord) : undefined;
}
