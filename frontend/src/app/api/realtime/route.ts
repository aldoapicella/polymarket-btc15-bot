import { NextResponse } from "next/server";
import WebSocket from "ws";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  if (process.env.BACKEND_SSE_URL) {
    return proxySse(process.env.BACKEND_SSE_URL);
  }

  const encoder = new TextEncoder();
  let ws: WebSocket | null = null;
  let heartbeat: ReturnType<typeof setInterval> | null = null;
  let closed = false;

  function cleanup() {
    if (closed) {
      return;
    }
    closed = true;
    if (heartbeat) {
      clearInterval(heartbeat);
      heartbeat = null;
    }
    ws?.removeAllListeners();
    ws?.close();
    ws = null;
  }

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (chunk: string) => {
        if (closed) {
          return;
        }
        try {
          controller.enqueue(encoder.encode(chunk));
        } catch {
          cleanup();
        }
      };
      const wsUrl = backendWebSocketUrl();
      ws = new WebSocket(wsUrl);
      heartbeat = setInterval(() => {
        send(": heartbeat\n\n");
      }, 15000);

      ws.on("message", (data) => {
        send(`data: ${data.toString()}\n\n`);
      });

      ws.on("open", () => {
        send("event: connected\ndata: {}\n\n");
      });

      ws.on("error", (error) => {
        send(`event: error\ndata: ${JSON.stringify({ detail: error.message })}\n\n`);
      });

      ws.on("close", () => {
        cleanup();
        try {
          controller.close();
        } catch {
          return;
        }
      });
    },
    cancel() {
      cleanup();
      return undefined;
    }
  });

  return new NextResponse(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive"
    }
  });
}

async function proxySse(sseUrl: string) {
  const headers = new Headers();
  const token = process.env.BACKEND_API_BEARER_TOKEN;
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  try {
    const response = await fetch(sseUrl, {
      headers,
      cache: "no-store"
    });
    if (!response.ok || !response.body) {
      return NextResponse.json(
        {
          detail: "Backend realtime stream is unavailable.",
          status: response.status
        },
        { status: 502 }
      );
    }
    return new NextResponse(response.body, {
      headers: {
        "Content-Type": response.headers.get("content-type") ?? "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive"
      }
    });
  } catch (error) {
    return NextResponse.json(
      {
        detail: "Backend realtime stream is unavailable.",
        error: error instanceof Error ? error.message : String(error)
      },
      { status: 502 }
    );
  }
}

function backendWebSocketUrl() {
  const explicit = process.env.BACKEND_WS_URL;
  const base = explicit || deriveWsUrl(process.env.BACKEND_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1");
  const url = new URL(base);
  const token = process.env.BACKEND_API_BEARER_TOKEN;
  if (token) {
    url.searchParams.set("token", token);
  }
  return url.toString();
}

function deriveWsUrl(apiBaseUrl: string) {
  const url = new URL(apiBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/$/, "")}/ws/live`;
  return url.toString();
}
