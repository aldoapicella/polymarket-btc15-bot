"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getMarketChart, getMarketDetail } from "@/lib/api";
import {
  emptyMarketSeries,
  formatChartTime,
  mergeRuntimeEventsIntoSeries,
  rangeLabel,
  type ChartRange,
  type ChartPoint
} from "@/lib/charting";
import { compact, dateTime, numberText, pctText } from "@/lib/format";
import { useRealtimeSnapshot } from "@/components/dashboard/useRealtimeSnapshot";
import { EmptyState, IconButton, InfoHint, Panel, PanelHeader, Pill } from "@/components/ui";

const RANGE_FILTERS: ChartRange[] = ["full", "5m", "1m"];

export function MarketDetailPage({ marketId }: { marketId: string }) {
  const queryClient = useQueryClient();
  const [range, setRange] = useState<ChartRange>("full");
  const eventTape = useRealtimeSnapshot(queryClient);
  const detail = useQuery({
    queryKey: ["markets", marketId],
    queryFn: () => getMarketDetail(marketId),
    refetchInterval: 10000
  });
  const chart = useQuery({
    queryKey: ["markets", "chart", marketId, range],
    queryFn: () => getMarketChart(marketId, range),
    refetchInterval: 30000
  });

  const data = detail.data;
  const market = data?.market;
  const series = mergeRuntimeEventsIntoSeries(chart.data ?? emptyMarketSeries(market, range), eventTape, marketId, market, range);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Link
            href="/markets"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-sm border border-line bg-white text-ink/70 hover:bg-panel"
            aria-label="Back to markets"
            title="Back to markets"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <div className="min-w-0">
            <h1 className="truncate text-xl font-semibold text-ink">{market?.question ?? marketId}</h1>
            <p className="truncate text-xs text-ink/50">{marketId}</p>
          </div>
        </div>
        <IconButton
          label="Refresh market"
          onClick={() => {
            detail.refetch();
            chart.refetch();
          }}
        >
          <RefreshCw className="h-4 w-4" />
        </IconButton>
      </div>

      {market ? (
        <>
          <div className="grid gap-3 md:grid-cols-4">
            <Metric label="Status" value={market.status} tone={market.is_active ? "good" : "neutral"} help="Market lifecycle status from discovery and the backend snapshot." />
            <Metric label="Start Price" value={compact(market.start_price)} help="Reference price captured at the market window start." />
            <Metric label="q Up" value={pctText(market.fair_value?.q_up)} help="Model-implied probability that this market resolves Up." />
            <Metric label="q Down" value={pctText(market.fair_value?.q_down)} help="Model-implied probability that this market resolves Down." />
          </div>

          <div className="grid gap-5 xl:grid-cols-[1fr_420px]">
            <Panel>
              <PanelHeader
                title="Order Books"
                meta={`${dateTime(market.start_ts)} -> ${dateTime(market.end_ts)}`}
                help="Top bid and ask levels for the UP and DOWN outcome tokens."
              />
              <div className="grid gap-4 p-4 md:grid-cols-2">
                <BookPanel title="Up Book" book={data.books.up ?? null} />
                <BookPanel title="Down Book" book={data.books.down ?? null} />
              </div>
            </Panel>

            <Panel>
              <PanelHeader
                title="Market Window Chart"
                meta={`${rangeLabel(range)} · ${series.marketChart.length} visible · ${series.sampleCount} stored`}
                help="Persisted market-specific probability and book samples. Full market pins the x-axis to the market start and end."
              >
                <RangeControl range={range} onChange={setRange} />
              </PanelHeader>
              <div className="h-72 p-4">
                {series.marketChart.length ? (
                  <MarketSeriesChart points={series.marketChart} domain={series.domain} />
                ) : (
                  <EmptyState label={chart.isLoading ? "Loading persisted samples" : "No stored samples for this range yet"} />
                )}
              </div>
            </Panel>
          </div>

          <div className="grid gap-5 xl:grid-cols-2">
            <ReferenceDistanceChart points={series.marketChart} domain={series.domain} />
            <PaperFillChart fills={series.fills} domain={series.domain} />
          </div>

          <div className="grid gap-5 xl:grid-cols-2">
            <Timeline title="Decisions" rows={data.decisions.map((row) => [row.action, compact(row.outcome), compact(row.price), row.reason])} />
            <Timeline title="Execution Reports" rows={data.execution_reports.map((row) => [row.status, compact(row.filled_size), compact(row.avg_price), dateTime(row.local_ts)])} />
          </div>
        </>
      ) : (
        <Panel>
          <EmptyState label={detail.isLoading ? "Loading market" : detail.error?.message ?? "Market unavailable"} />
        </Panel>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  tone = "neutral",
  help
}: {
  label: string;
  value: string;
  tone?: "neutral" | "good" | "warn" | "danger";
  help?: string;
}) {
  return (
    <Panel className="p-4">
      <div className="flex items-center gap-1 text-xs font-medium uppercase text-ink/50">
        <span>{label}</span>
        {help ? <InfoHint label={help} /> : null}
      </div>
      <div className="mt-1 flex items-center gap-2">
        <span className="truncate text-lg font-semibold text-ink">{value}</span>
        <Pill tone={tone}>{tone}</Pill>
      </div>
    </Panel>
  );
}

function RangeControl({ range, onChange }: { range: ChartRange; onChange: (range: ChartRange) => void }) {
  return (
    <div className="flex shrink-0 flex-wrap gap-1">
      {RANGE_FILTERS.map((item) => (
        <button
          key={item}
          className={[
            "h-7 rounded-sm border px-2 text-[11px] font-semibold transition",
            range === item ? "border-good bg-good text-white" : "border-line bg-white text-ink/65 hover:bg-panel"
          ].join(" ")}
          onClick={() => onChange(item)}
        >
          {rangeLabel(item)}
        </button>
      ))}
    </div>
  );
}

function MarketSeriesChart({ points, domain }: { points: ChartPoint[]; domain: [number, number] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={points}>
        <CartesianGrid stroke="#d9ddd2" strokeDasharray="3 3" />
        <XAxis dataKey="bucket" type="number" domain={domain} tick={{ fontSize: 11 }} minTickGap={20} tickFormatter={formatChartTime} />
        <YAxis domain={[0, 1]} tick={{ fontSize: 11 }} width={32} />
        <Tooltip formatter={(value) => numberText(value, 3)} />
        <Legend />
        <Line type="monotone" dataKey="qUp" name="q Up" stroke="#18705b" dot={false} strokeWidth={2.1} connectNulls isAnimationActive={false} />
        <Line type="monotone" dataKey="qDown" name="q Down" stroke="#b3363a" dot={false} strokeWidth={2.1} connectNulls isAnimationActive={false} />
        <Line type="monotone" dataKey="upBid" name="UP bid" stroke="#2f7fcb" dot={false} strokeWidth={1.3} connectNulls isAnimationActive={false} />
        <Line type="monotone" dataKey="upAsk" name="UP ask" stroke="#74a8dd" dot={false} strokeWidth={1.3} strokeDasharray="4 4" connectNulls isAnimationActive={false} />
        <Line type="monotone" dataKey="downBid" name="DOWN bid" stroke="#a45d13" dot={false} strokeWidth={1.3} connectNulls isAnimationActive={false} />
        <Line type="monotone" dataKey="downAsk" name="DOWN ask" stroke="#d49a4e" dot={false} strokeWidth={1.3} strokeDasharray="4 4" connectNulls isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

function ReferenceDistanceChart({ points, domain }: { points: ChartPoint[]; domain: [number, number] }) {
  const rows = points.filter((point) => Number.isFinite(point.distanceBps));
  return (
    <Panel>
      <PanelHeader
        title="Reference Distance"
        meta={`${rows.length} samples`}
        help="Reference price move from the market start price, measured in basis points."
      />
      <div className="h-64 p-4">
        {rows.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows}>
              <CartesianGrid stroke="#d9ddd2" strokeDasharray="3 3" />
              <XAxis dataKey="bucket" type="number" domain={domain} tick={{ fontSize: 11 }} minTickGap={24} tickFormatter={formatChartTime} />
              <YAxis tick={{ fontSize: 11 }} width={42} tickFormatter={(value) => `${value}`} />
              <Tooltip formatter={(value) => `${numberText(value, 1)} bps`} />
              <ReferenceLine y={0} stroke="#17201b" strokeOpacity={0.35} />
              <Line type="monotone" dataKey="distanceBps" name="distance" stroke="#18705b" dot={false} strokeWidth={2} connectNulls isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <EmptyState label="No reference samples for this range" />
        )}
      </div>
    </Panel>
  );
}

function PaperFillChart({ fills, domain }: { fills: ChartPoint[]; domain: [number, number] }) {
  return (
    <Panel>
      <PanelHeader
        title="Paper Fills"
        meta={`${fills.length} fills`}
        help="Simulated paper maker fills plotted by fill price inside the selected market range."
      />
      <div className="h-64 p-4">
        {fills.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={fills}>
              <CartesianGrid stroke="#d9ddd2" strokeDasharray="3 3" />
              <XAxis dataKey="bucket" type="number" domain={domain} tick={{ fontSize: 11 }} minTickGap={24} tickFormatter={formatChartTime} />
              <YAxis domain={[0, 1]} tick={{ fontSize: 11 }} width={32} />
              <Tooltip formatter={(value) => numberText(value, 2)} />
              <Line type="monotone" dataKey="fillPrice" name="fill price" stroke="#a45d13" dot={{ r: 3 }} strokeWidth={0} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <EmptyState label="No paper fills for this range" />
        )}
      </div>
    </Panel>
  );
}

function BookPanel({ title, book }: { title: string; book: { bids: { price: string; size: string }[]; asks: { price: string; size: string }[]; local_ts: string } | null }) {
  const rows = [
    ...(book?.bids ?? []).slice(0, 5).map((row) => ({ ...row, side: "Bid" })),
    ...(book?.asks ?? []).slice(0, 5).map((row) => ({ ...row, side: "Ask" }))
  ];
  return (
    <div className="border border-line bg-panel">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        <span className="text-xs text-ink/50">{book ? dateTime(book.local_ts) : "n/a"}</span>
      </div>
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-ink/50">
          <tr>
            <th className="px-3 py-2">Side</th>
            <th className="px-3 py-2">Price</th>
            <th className="px-3 py-2">Size</th>
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((row, index) => (
            <tr key={`${row.side}-${index}`} className="border-t border-line">
              <td className="px-3 py-2">{row.side}</td>
              <td className="px-3 py-2">{numberText(row.price, 3)}</td>
              <td className="px-3 py-2">{numberText(row.size, 2)}</td>
            </tr>
          )) : (
            <tr><td colSpan={3}><EmptyState label="No book levels" /></td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function Timeline({ title, rows }: { title: string; rows: string[][] }) {
  return (
    <Panel>
      <PanelHeader title={title} meta={`${rows.length} rows`} />
      <div className="max-h-80 overflow-auto">
        {rows.length ? rows.slice().reverse().map((row, index) => (
          <div key={index} className="grid grid-cols-[120px_80px_80px_1fr] gap-3 border-b border-line px-4 py-3 text-sm last:border-b-0">
            {row.map((cell, cellIndex) => (
              <span key={cellIndex} className={cellIndex === 3 ? "truncate text-ink/60" : "truncate text-ink"}>{cell}</span>
            ))}
          </div>
        )) : (
          <EmptyState label={`No ${title.toLowerCase()}`} />
        )}
      </div>
    </Panel>
  );
}
