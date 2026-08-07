export function fmtUsd(v: number | null | undefined, opts: { sign?: boolean; decimals?: number } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const decimals = opts.decimals ?? (Math.abs(v) >= 100 ? 0 : 2);
  const sign = opts.sign && v > 0 ? "+" : "";
  return `${sign}$${v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`;
}

export function fmtPct(v: number | null | undefined, decimals = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v.toFixed(decimals)}%`;
}

export function fmtNum(v: number | null | undefined, decimals = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("en-US", { maximumFractionDigits: decimals });
}

export function fmtMs(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (v < 1000) return `${v.toFixed(0)}ms`;
  return `${(v / 1000).toFixed(2)}s`;
}

export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString("en-US", { hour12: false });
}

export function fmtDateTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtAgo(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m ago`;
  return `${Math.floor(h / 24)}d ${h % 24}h ago`;
}

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  const total = Math.max(0, Math.round(s));
  const m = Math.floor(total / 60);
  const sec = total % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

export function fmtUptime(s: number | null | undefined): string {
  if (!s) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${Math.floor(s % 60)}s`;
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  return id.length > 10 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id;
}

export function pnlColor(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "text-ink-muted";
  return v > 0 ? "text-ok" : "text-danger";
}
