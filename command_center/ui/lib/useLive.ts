"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

/**
 * Keeps the app feeling alive: a single WebSocket connection to the FastAPI
 * backend pushes the full overview snapshot every ~2s, which we write straight
 * into the React Query cache (no extra REST traffic). If the socket drops, the
 * existing polling intervals take over seamlessly and we keep retrying.
 */
export function useLiveWs() {
  const qc = useQueryClient();

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let alive = true;

    const connect = () => {
      if (!alive) return;
      // Derive the host so the stream survives opening the UI from another
      // device on the LAN (REST already works via the rewrite; this keeps WS
      // on the same host). Falls back to loopback.
      const host = window.location.hostname || "127.0.0.1";
      ws = new WebSocket(`ws://${host}:8787/ws/live`);
      ws.onopen = () => {
        retry = 0;
        qc.setQueryData(["ws"], { connected: true, ts: Date.now() });
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(String(e.data));
          if (msg.type === "snapshot" && msg.overview) {
            qc.setQueryData(["overview"], msg.overview);
            qc.setQueryData(["ws"], { connected: true, ts: Date.now() });
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        qc.setQueryData(["ws"], { connected: false, ts: Date.now() });
        if (alive) {
          const delay = Math.min(6000, 600 * 2 ** retry);
          retry += 1;
          setTimeout(connect, delay);
        }
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      alive = false;
      ws?.close();
    };
  }, [qc]);
}

export function useWsStatus(): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const update = () => {
      const data = qc.getQueryData<{ connected?: boolean }>(["ws"]);
      setConnected(!!data?.connected);
    };
    update();
    const unsub = qc.getQueryCache().subscribe((event) => {
      if (event.query.queryKey[0] === "ws") update();
    });
    return () => unsub();
  }, [qc]);

  return connected;
}
