"use client";

import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { MARKET_EVENT_BUFFER_LIMIT } from "@/lib/charting";
import type { RuntimeEvent, Snapshot } from "@/lib/types";

const SNAPSHOT_EVENT_TYPES = new Set(["status_snapshot", "ui_snapshot"]);
const REFRESH_EVENT_TYPES = new Set([
  "paper_fill",
  "paper_settlement",
  "kill_switch_changed",
  "control_state_changed",
  "config_changed",
  "report_job_update",
  "execution_report"
]);

export function useRealtimeSnapshot(queryClient: QueryClient) {
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const eventBufferRef = useRef<RuntimeEvent[]>([]);
  const pendingSnapshotRef = useRef<Snapshot | null>(null);
  const pendingRefreshRef = useRef(false);

  useEffect(() => {
    const stream = new EventSource("/api/realtime");
    const flush = window.setInterval(() => {
      setEvents([...eventBufferRef.current]);
      if (pendingSnapshotRef.current) {
        queryClient.setQueryData(["snapshot"], pendingSnapshotRef.current);
        pendingSnapshotRef.current = null;
      }
      if (pendingRefreshRef.current) {
        queryClient.invalidateQueries({ queryKey: ["snapshot"] });
        pendingRefreshRef.current = false;
      }
    }, 1000);

    stream.onmessage = (message) => {
      const event = parseRuntimeEvent(message.data);
      if (!event) {
        return;
      }
      eventBufferRef.current = [event, ...eventBufferRef.current].slice(0, MARKET_EVENT_BUFFER_LIMIT);
      if (SNAPSHOT_EVENT_TYPES.has(event.type)) {
        pendingSnapshotRef.current = event.data as Snapshot;
      }
      if (REFRESH_EVENT_TYPES.has(event.type)) {
        pendingRefreshRef.current = true;
      }
    };
    stream.onerror = () => undefined;
    return () => {
      window.clearInterval(flush);
      stream.close();
    };
  }, [queryClient]);

  return events;
}

function parseRuntimeEvent(data: string): RuntimeEvent | null {
  try {
    const parsed = JSON.parse(data) as RuntimeEvent & { event_type?: unknown };
    const type = typeof parsed.type === "string" ? parsed.type : typeof parsed.event_type === "string" ? parsed.event_type : "";
    if (!type) {
      return null;
    }
    return {
      ...parsed,
      type,
      data: parsed.data && typeof parsed.data === "object" && !Array.isArray(parsed.data) ? parsed.data : {}
    } as RuntimeEvent;
  } catch {
    return null;
  }
}
