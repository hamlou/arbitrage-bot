"use client";

import { useQuery } from "@tanstack/react-query";
import type {
  ActivityItem,
  EquityPoint,
  LatencySummary,
  MarketRow,
  Overview,
  PositionRow,
  SignalRow,
  Trade,
  TradesResponse,
} from "./types";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json() as Promise<T>;
}

// -- overview (the home screen payload) --------------------------------------

export function useOverview(intervalMs = 2500) {
  return useQuery({
    queryKey: ["overview"],
    queryFn: () => get<Overview>("/api/overview"),
    refetchInterval: intervalMs,
    refetchIntervalInBackground: false,
    staleTime: intervalMs - 400,
  });
}

export function useHealth(intervalMs = 5000) {
  return useQuery({
    queryKey: ["health"],
    queryFn: () =>
      get<{ bot_status: string; state_file_age_s: number | null; db: string }>("/api/health"),
    refetchInterval: intervalMs,
  });
}

// -- trades ------------------------------------------------------------------

export function useTrades(params: Record<string, string | undefined>, enabled = true) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== "" && v !== "all") qs.set(k, v);
  });
  return useQuery({
    queryKey: ["trades", qs.toString()],
    queryFn: () => get<TradesResponse>(`/api/trades?${qs.toString()}`),
    refetchInterval: 5000,
    enabled,
  });
}

// -- equity / charts ---------------------------------------------------------

export function useEquity(limit = 3000) {
  return useQuery({
    queryKey: ["equity", limit],
    queryFn: () => get<{ points: EquityPoint[] }>(`/api/equity?limit=${limit}`),
    refetchInterval: 8000,
  });
}

export function useLatency() {
  return useQuery({
    queryKey: ["latency"],
    queryFn: () =>
      get<{
        count: number;
        fired: number;
        tick_to_signal: Record<string, number | null>;
        tick_to_order: Record<string, number | null>;
        signal_to_order: Record<string, number | null>;
        series: { id: number; tick_to_signal_ms: number | null; tick_to_order_ms: number | null; fired: boolean; market_id?: string }[];
      }>("/api/latency"),
    refetchInterval: 8000,
  });
}

export function usePositions() {
  return useQuery({
    queryKey: ["positions"],
    queryFn: () => get<{ positions: PositionRow[]; count: number }>("/api/positions"),
    refetchInterval: 2500,
  });
}

export function useActivity() {
  return useQuery({
    queryKey: ["activity"],
    queryFn: () => get<{ items: ActivityItem[]; count: number }>("/api/activity?limit=120"),
    refetchInterval: 6000,
  });
}

export function useSignals(firedOnly = false, limit = 200) {
  return useQuery({
    queryKey: ["signals", firedOnly, limit],
    queryFn: () =>
      get<{ signals: SignalRow[]; count: number; fired: number; avg_edge_pct: number | null }>(
        `/api/signals?limit=${limit}${firedOnly ? "&fired=true" : ""}`
      ),
    refetchInterval: 8000,
  });
}

export function useMarkets() {
  return useQuery({
    queryKey: ["markets"],
    queryFn: () => get<{ markets: MarketRow[]; count: number }>("/api/markets"),
    refetchInterval: 2500,
  });
}

export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    queryFn: () => get<{ config: Record<string, unknown> }>("/api/config"),
    refetchInterval: 30_000,
  });
}

export function useRiskEvents() {
  return useQuery({
    queryKey: ["risk-events"],
    queryFn: () => get<{ events: unknown[]; count: number }>("/api/risk-events?limit=50"),
    refetchInterval: 10_000,
  });
}

