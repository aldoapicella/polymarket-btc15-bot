import type { JsonRecord, MarketSummary, RuntimeEvent } from "@/lib/types";

export const MARKET_EVENT_BUFFER_LIMIT = 5000;
export const MAX_VISIBLE_CHART_POINTS = 900;

const STREAM_BUCKET_MS = 1000;

export type ChartRange = "full" | "5m" | "1m";

export type ChartPoint = {
  bucket: number;
  time: string;
  qUp?: number;
  qDown?: number;
  upBid?: number;
  upAsk?: number;
  downBid?: number;
  downAsk?: number;
  distanceBps?: number;
  referencePrice?: number;
  fillPrice?: number;
  fillOutcome?: string;
  fillSize?: number;
};

export type MarketSeries = {
  source?: string;
  warning?: string | null;
  market_id?: string;
  range?: ChartRange;
  marketChart: ChartPoint[];
  fills: ChartPoint[];
  domain: [number, number];
  sampleCount: number;
};

export function emptyMarketSeries(market?: MarketSummary | null, range: ChartRange = "full"): MarketSeries {
  return {
    range,
    marketChart: [],
    fills: [],
    domain: marketWindowDomain(market, Date.now()),
    sampleCount: 0
  };
}

export function thinChartPoints(points: ChartPoint[], maxPoints = MAX_VISIBLE_CHART_POINTS): ChartPoint[] {
  if (points.length <= maxPoints) {
    return points;
  }
  const keep = new Set<number>([0, points.length - 1]);
  for (const [index, point] of points.entries()) {
    if (point.fillPrice !== undefined || point.fillSize !== undefined) {
      keep.add(index);
    }
  }
  const slots = Math.max(0, maxPoints - keep.size);
  if (slots > 0) {
    const stride = (points.length - 1) / slots;
    for (let index = 0; index < slots; index += 1) {
      keep.add(Math.round(index * stride));
    }
  }
  return [...keep]
    .sort((left, right) => left - right)
    .map((index) => points[index])
    .filter((point): point is ChartPoint => point !== undefined);
}

export function mergeRuntimeEventsIntoSeries(
  base: MarketSeries,
  events: RuntimeEvent[],
  marketId?: string | null,
  market?: MarketSummary | null,
  range: ChartRange = base.range ?? "full"
): MarketSeries {
  if (!marketId || events.length === 0) {
    return {
      ...base,
      marketChart: thinChartPoints(base.marketChart),
      fills: base.fills.length ? thinChartPoints(base.fills, 300) : base.marketChart.filter(hasFill)
    };
  }
  const merged = new Map<number, ChartPoint>();
  for (const point of base.marketChart) {
    merged.set(point.bucket, { ...point });
  }
  for (const event of [...events].reverse()) {
    const point = chartPointFromRuntimeEvent(event, marketId);
    if (!point) {
      continue;
    }
    const prior = merged.get(point.bucket);
    merged.set(point.bucket, prior ? mergePoint(prior, point) : point);
  }
  let marketChart = [...merged.values()].sort((left, right) => left.bucket - right.bucket);
  marketChart = filterRange(marketChart, range);
  const visible = thinChartPoints(marketChart);
  const fills = visible.filter(hasFill);
  return {
    ...base,
    range,
    market_id: base.market_id ?? marketId,
    marketChart: visible,
    fills,
    domain: range === "full" ? base.domain ?? marketWindowDomain(market, Date.now()) : derivedDomain(marketChart) ?? base.domain,
    sampleCount: Math.max(base.sampleCount, marketChart.length)
  };
}

export function formatChartTime(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "";
  }
  return new Date(numeric).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function rangeLabel(range: ChartRange) {
  if (range === "full") {
    return "Full market";
  }
  return `Last ${range}`;
}

function marketWindowDomain(market: MarketSummary | null | undefined, now: number): [number, number] {
  const start = parseTs(market?.start_ts) ?? now - 15 * 60 * 1000;
  const end = parseTs(market?.end_ts) ?? now;
  return end > start ? [start, end] : [start, start + 15 * 60 * 1000];
}

function chartPointFromRuntimeEvent(event: RuntimeEvent, marketId: string): ChartPoint | null {
  if (eventMarketId(event.data) !== marketId) {
    return null;
  }
  if (event.type === "fair_value_update") {
    const bucket = eventBucket(event.data, event.ts, "computed_ts");
    return bucket === undefined
      ? null
      : {
          bucket,
          time: new Date(bucket).toISOString(),
          qUp: numeric(event.data.q_up),
          qDown: numeric(event.data.q_down)
        };
  }
  if (event.type === "book_update_summary") {
    const bucket = eventBucket(event.data, event.ts, "local_ts");
    if (bucket === undefined) {
      return null;
    }
    const point: ChartPoint = {
      bucket,
      time: new Date(bucket).toISOString()
    };
    const outcome = text(event.data.outcome);
    if (outcome === "up") {
      point.upBid = priceValue(event.data.best_bid);
      point.upAsk = priceValue(event.data.best_ask);
    } else if (outcome === "down") {
      point.downBid = priceValue(event.data.best_bid);
      point.downAsk = priceValue(event.data.best_ask);
    }
    return point;
  }
  if (event.type === "paper_fill" || event.type === "execution_report") {
    const bucket = eventBucket(event.data, event.ts, "local_ts");
    const filledSize = numeric(event.data.filled_size);
    const fillPrice = numeric(event.data.avg_price);
    if (bucket === undefined || fillPrice === undefined || (filledSize ?? 0) <= 0) {
      return null;
    }
    return {
      bucket,
      time: new Date(bucket).toISOString(),
      fillPrice,
      fillSize: filledSize,
      fillOutcome: text(event.data.outcome) ?? text(record(event.data.raw)?.outcome)
    };
  }
  return null;
}

function eventMarketId(data: JsonRecord) {
  return text(data.market_id) ?? text(data.marketId);
}

function eventBucket(data: JsonRecord, eventTs: string, preferredTs: string) {
  const raw = timestamp(data[preferredTs]) ?? timestamp(data.ts) ?? timestamp(eventTs);
  if (raw === undefined) {
    return undefined;
  }
  return Math.floor(raw / STREAM_BUCKET_MS) * STREAM_BUCKET_MS;
}

function filterRange(points: ChartPoint[], range: ChartRange) {
  const windowMs = rangeWindowMs(range);
  if (!windowMs || points.length === 0) {
    return points;
  }
  const last = points[points.length - 1]?.bucket;
  if (last === undefined) {
    return points;
  }
  const cutoff = last - windowMs;
  return points.filter((point) => point.bucket >= cutoff);
}

function rangeWindowMs(range: ChartRange) {
  if (range === "1m") {
    return 60_000;
  }
  if (range === "5m") {
    return 5 * 60_000;
  }
  return undefined;
}

function derivedDomain(points: ChartPoint[]): [number, number] | undefined {
  if (points.length === 0) {
    return undefined;
  }
  const start = points[0]?.bucket;
  const end = points[points.length - 1]?.bucket;
  return start !== undefined && end !== undefined && end > start ? [start, end] : undefined;
}

function mergePoint(left: ChartPoint, right: ChartPoint): ChartPoint {
  return {
    ...left,
    ...definedFields(right),
    bucket: left.bucket,
    time: left.time || right.time
  };
}

function definedFields(point: ChartPoint) {
  return Object.fromEntries(Object.entries(point).filter(([, value]) => value !== undefined));
}

function hasFill(point: ChartPoint) {
  return point.fillPrice !== undefined;
}

function priceValue(value: unknown) {
  const direct = numeric(value);
  if (direct !== undefined) {
    return direct;
  }
  return numeric(record(value)?.price);
}

function parseTs(value: string | undefined | null) {
  if (!value) {
    return undefined;
  }
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : undefined;
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

function record(value: unknown): JsonRecord | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonRecord) : undefined;
}
