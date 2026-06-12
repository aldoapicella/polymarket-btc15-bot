"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getLatestReport, getMarketChart, getSnapshot } from "@/lib/api";
import { emptyMarketSeries, mergeRuntimeEventsIntoSeries } from "@/lib/charting";
import { ageText } from "@/lib/format";
import { ActiveMarketPanel } from "@/components/dashboard/ActiveMarketPanel";
import { ControlPanel } from "@/components/dashboard/ControlPanel";
import { DecisionTable } from "@/components/dashboard/DecisionTable";
import { EventTimeline } from "@/components/dashboard/EventTimeline";
import { ExecutionReportTable } from "@/components/dashboard/ExecutionReportTable";
import { MarketMainChart, TrendCharts } from "@/components/dashboard/MarketCharts";
import { DashboardHeader, SystemHealthCards } from "@/components/dashboard/SystemStatus";
import { recorderSummary } from "@/components/dashboard/model";
import { useRealtimeSnapshot } from "@/components/dashboard/useRealtimeSnapshot";

export function Dashboard() {
  const queryClient = useQueryClient();
  const snapshot = useQuery({
    queryKey: ["snapshot"],
    queryFn: getSnapshot,
    refetchInterval: 10000
  });
  const latestReport = useQuery({
    queryKey: ["reports", "latest"],
    queryFn: getLatestReport,
    retry: false,
    refetchInterval: 30000
  });
  const eventTape = useRealtimeSnapshot(queryClient);

  const snapshotStore = snapshot.data;
  const status = snapshotStore?.status;
  const active = snapshotStore?.current_market;
  const reference = status?.reference;
  const reportSummary = latestReport.data?.report?.summary;
  const killSwitchOn = Boolean(status?.kill_switch);
  const paused = Boolean(status?.control?.paused);
  const recorder = recorderSummary(status?.recorder);
  const chartSeries = useQuery({
    queryKey: ["markets", "chart", active?.market_id ?? "none", "full"],
    queryFn: () => getMarketChart(active?.market_id ?? "", "full"),
    enabled: Boolean(active?.market_id),
    refetchInterval: 30000
  });
  const seriesStore = mergeRuntimeEventsIntoSeries(
    chartSeries.data ?? emptyMarketSeries(active),
    eventTape,
    active?.market_id,
    active,
    "full"
  );

  return (
    <div className="space-y-5">
      <DashboardHeader
        mode={status?.execution_mode}
        referenceFresh={!reference?.stale}
        recorderHealthy={recorder.healthy}
        onRefresh={() => queryClient.invalidateQueries({ queryKey: ["snapshot"] })}
      />

      <SystemHealthCards
        status={status}
        reportSummary={reportSummary}
        recorder={recorder}
        killSwitchOn={killSwitchOn}
        paused={paused}
      />

      <ControlPanel
        killSwitchOn={killSwitchOn}
        paused={paused}
        reportPending={latestReport.isFetching}
        onAfterAction={() => {
          queryClient.invalidateQueries({ queryKey: ["snapshot"] });
          queryClient.invalidateQueries({ queryKey: ["reports", "latest"] });
        }}
      />

      <div className="grid gap-5 xl:grid-cols-12">
        <ActiveMarketPanel
          active={active}
          referencePrice={reference?.price}
          referenceAge={ageText(reference?.local_ts)}
          isLoading={snapshot.isLoading}
        />
        <MarketMainChart points={seriesStore.marketChart} domain={seriesStore.domain} sampleCount={seriesStore.sampleCount} />
      </div>

      <TrendCharts points={seriesStore.marketChart} fills={seriesStore.fills} domain={seriesStore.domain} />

      <div className="grid gap-5 xl:grid-cols-12">
        <div className="min-w-0 xl:col-span-5">
          <DecisionTable decisions={snapshotStore?.latest_decisions ?? []} />
        </div>
        <div className="min-w-0 xl:col-span-7">
          <EventTimeline events={eventTape} active={active} />
        </div>
      </div>

      <ExecutionReportTable reports={snapshotStore?.latest_execution_reports ?? []} active={active} />
    </div>
  );
}
