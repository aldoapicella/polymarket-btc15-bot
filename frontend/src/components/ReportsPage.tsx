"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileJson, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { buildReport, getDailyReport, getLatestReport } from "@/lib/api";
import type { ReportPayload } from "@/lib/types";
import { compact, dateTime, numberText } from "@/lib/format";
import { Button, EmptyState, IconButton, Panel, PanelHeader, Pill } from "@/components/ui";

export function ReportsPage() {
  const queryClient = useQueryClient();
  const today = new Date().toISOString().slice(0, 10);
  const [date, setDate] = useState(today);
  const [prefix, setPrefix] = useState("");
  const [force, setForce] = useState(false);
  const [baselineDate, setBaselineDate] = useState(today);
  const [candidateDate, setCandidateDate] = useState(today);
  const latest = useQuery({
    queryKey: ["reports", "latest"],
    queryFn: getLatestReport,
    retry: false
  });
  const daily = useQuery({
    queryKey: ["reports", "daily", date],
    queryFn: () => getDailyReport(date),
    enabled: false,
    retry: false
  });
  const baseline = useQuery({
    queryKey: ["reports", "compare", "baseline", baselineDate],
    queryFn: () => getDailyReport(baselineDate),
    enabled: false,
    retry: false
  });
  const candidate = useQuery({
    queryKey: ["reports", "compare", "candidate", candidateDate],
    queryFn: () => getDailyReport(candidateDate),
    enabled: false,
    retry: false
  });
  const build = useMutation({
    mutationFn: () =>
      buildReport({
        source: "auto",
        date: prefix ? null : date,
        prefix: prefix || null,
        force,
        settlement_window_seconds: 15
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reports"] });
    }
  });

  const selected = build.data ?? daily.data ?? latest.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Reports</h1>
        </div>
        <IconButton label="Refresh reports" onClick={() => queryClient.invalidateQueries({ queryKey: ["reports"] })}>
          <RefreshCw className="h-4 w-4" />
        </IconButton>
      </div>

      <div className="grid gap-5 xl:grid-cols-[420px_1fr]">
        <Panel>
          <PanelHeader title="Build Report" meta="Daily report or Azure prefix" />
          <div className="space-y-4 p-4">
            <label className="block">
              <span className="text-xs font-medium text-ink/60">Report date</span>
              <input
                type="date"
                value={date}
                onChange={(event) => setDate(event.target.value)}
                className="mt-1 h-10 w-full rounded-sm border border-line bg-white px-3 text-sm text-ink"
              />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-ink/60">Azure prefix</span>
              <input
                value={prefix}
                onChange={(event) => setPrefix(event.target.value)}
                placeholder="events/YYYY/MM/DD/"
                className="mt-1 h-10 w-full rounded-sm border border-line bg-white px-3 text-sm text-ink"
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-ink/70">
              <input
                type="checkbox"
                checked={force}
                onChange={(event) => setForce(event.target.checked)}
                className="h-4 w-4 accent-good"
              />
              Force rebuild
            </label>
            {build.error ? <p className="text-sm text-danger">{build.error.message}</p> : null}
            <div className="flex flex-wrap gap-2">
              <Button disabled={build.isPending} onClick={() => build.mutate()}>
                <FileJson className="h-4 w-4" />
                Build
              </Button>
              <Button disabled={daily.isFetching} onClick={() => daily.refetch()}>
                <Search className="h-4 w-4" />
                View Daily
              </Button>
            </div>
          </div>
        </Panel>

        <ReportSummary report={selected} loading={latest.isLoading || daily.isFetching || build.isPending} />
      </div>

      <Panel>
        <PanelHeader title="Report Comparison" meta="Daily cached reports" />
        <div className="grid gap-4 p-4 lg:grid-cols-[360px_1fr]">
          <div className="space-y-3">
            <label className="block">
              <span className="text-xs font-medium text-ink/60">Baseline date</span>
              <input
                type="date"
                value={baselineDate}
                onChange={(event) => setBaselineDate(event.target.value)}
                className="mt-1 h-10 w-full rounded-sm border border-line bg-white px-3 text-sm text-ink"
              />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-ink/60">Candidate date</span>
              <input
                type="date"
                value={candidateDate}
                onChange={(event) => setCandidateDate(event.target.value)}
                className="mt-1 h-10 w-full rounded-sm border border-line bg-white px-3 text-sm text-ink"
              />
            </label>
            <Button disabled={baseline.isFetching || candidate.isFetching} onClick={() => { baseline.refetch(); candidate.refetch(); }}>
              <Search className="h-4 w-4" />
              Compare
            </Button>
          </div>
          <ComparisonTable baseline={baseline.data} candidate={candidate.data} loading={baseline.isFetching || candidate.isFetching} />
        </div>
      </Panel>
    </div>
  );
}

function ComparisonTable({ baseline, candidate, loading }: { baseline?: ReportPayload; candidate?: ReportPayload; loading: boolean }) {
  const rows: [string, unknown, unknown][] = [
    ["Actual paper net", baseline?.report?.summary?.actual_paper_net_pnl, candidate?.report?.summary?.actual_paper_net_pnl],
    ["Replay net", baseline?.report?.summary?.replay_estimate_net_pnl, candidate?.report?.summary?.replay_estimate_net_pnl],
    ["Runtime minus replay", baseline?.report?.summary?.runtime_minus_replay_pnl, candidate?.report?.summary?.runtime_minus_replay_pnl],
    ["Runtime fills", baseline?.report?.runtime_vs_replay?.runtime_filled_reports, candidate?.report?.runtime_vs_replay?.runtime_filled_reports],
    ["Replay fills", baseline?.report?.runtime_vs_replay?.replay_filled_orders, candidate?.report?.runtime_vs_replay?.replay_filled_orders]
  ];
  if (!baseline?.report || !candidate?.report) {
    return <EmptyState label={loading ? "Loading reports" : "Select reports to compare"} />;
  }
  return (
    <div className="overflow-auto border border-line">
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead className="border-b border-line bg-panel text-xs uppercase text-ink/50">
          <tr>
            <th className="px-3 py-2">Metric</th>
            <th className="px-3 py-2">Baseline</th>
            <th className="px-3 py-2">Candidate</th>
            <th className="px-3 py-2">Δ</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, left, right]) => (
            <tr key={label} className="border-b border-line last:border-b-0">
              <td className="px-3 py-2 font-medium">{label}</td>
              <td className="px-3 py-2">{numberText(left)}</td>
              <td className="px-3 py-2">{numberText(right)}</td>
              <td className="px-3 py-2">{numberText(delta(left, right))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function delta(left: unknown, right: unknown) {
  const l = Number(left);
  const r = Number(right);
  return Number.isFinite(l) && Number.isFinite(r) ? r - l : null;
}

function ReportSummary({ report, loading }: { report?: ReportPayload; loading: boolean }) {
  const summary = report?.report?.summary;
  const comparison = report?.report?.runtime_vs_replay;
  const job = report?.job ?? report?.report?.report_job;
  const cards = useMemo<[string, unknown][]>(
    () => [
      ["Actual Paper", summary?.actual_paper_net_pnl],
      ["Replay", summary?.replay_estimate_net_pnl],
      ["Runtime Δ", summary?.runtime_minus_replay_pnl],
      ["Filled Reports", comparison?.runtime_filled_reports]
    ],
    [summary, comparison]
  );

  return (
    <Panel>
      <PanelHeader title="Cached Report" meta={job ? `${job.job_id} · ${job.status}` : "Latest or selected daily report"}>
        {report ? (
          <Button onClick={() => downloadJson(report)}>
            <Download className="h-4 w-4" />
            JSON
          </Button>
        ) : null}
      </PanelHeader>
      {report?.report ? (
        <div className="space-y-4 p-4">
          <div className="flex flex-wrap gap-2">
            <Pill tone={job?.status === "completed" ? "good" : job?.status === "failed" ? "danger" : "warn"}>
              {job?.status ?? "unknown"}
            </Pill>
            <Pill>{job?.source ?? "source n/a"}</Pill>
            <Pill>{job?.finished_ts ? dateTime(job.finished_ts) : "unfinished"}</Pill>
          </div>
          <div className="grid gap-3 md:grid-cols-4">
            {cards.map(([label, value]) => (
              <div key={label} className="border border-line bg-panel px-3 py-3">
                <div className="truncate text-xs text-ink/50">{label}</div>
                <div className="mt-1 truncate text-lg font-semibold text-ink">{numberText(value)}</div>
              </div>
            ))}
          </div>
          <div className="overflow-auto border border-line">
            <table className="w-full min-w-[640px] text-left text-sm">
              <tbody>
                {Object.entries(summary ?? {}).slice(0, 12).map(([key, value]) => (
                  <tr key={key} className="border-b border-line last:border-b-0">
                    <th className="w-72 bg-panel px-3 py-2 text-xs font-medium uppercase text-ink/50">{key}</th>
                    <td className="px-3 py-2 text-ink">{compact(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <EmptyState label={loading ? "Loading cached report" : "No cached report loaded yet"} />
      )}
    </Panel>
  );
}

function downloadJson(payload: ReportPayload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `polyedge-report-${new Date().toISOString().slice(0, 19)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}
