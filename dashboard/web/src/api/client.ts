const API_KEY_STORAGE = "dashboard.api_key";

export function getApiKey(): string | null {
  return localStorage.getItem(API_KEY_STORAGE);
}

export function setApiKey(key: string): void {
  localStorage.setItem(API_KEY_STORAGE, key);
}

export function clearApiKey(): void {
  localStorage.removeItem(API_KEY_STORAGE);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const key = getApiKey();
  if (key) headers.set("X-API-Key", key);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) {
    clearApiKey();
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => request<{ status: string; db_ok: boolean; decisions_log_ok: boolean }>("/api/health"),
  summary: () => request<Summary>("/api/summary"),
  equitySeries: (since = 0) =>
    request<EquityPoint[]>(`/api/summary/equity_series?since=${since}&buckets=200`),
  positions: (status: "open" | "closed" | "all" = "open") =>
    request<Position[]>(`/api/positions?status=${status}&limit=500`),
  traders: (sort: "score" | "roi" | "pnl" | "trades" = "score") =>
    request<Trader[]>(`/api/traders?sort=${sort}&limit=500`),
  decisions: (since_offset = 0, type?: string) => {
    const t = type ? `&type=${encodeURIComponent(type)}` : "";
    return request<DecisionsPage>(`/api/decisions?since_offset=${since_offset}&limit=500${t}`);
  },
  setHalt: (reason: string) =>
    request<{ ok: boolean }>("/api/halt", { method: "POST", body: JSON.stringify({ reason }) }),
  clearHalt: () => request<{ ok: boolean }>("/api/halt", { method: "DELETE" }),
  setCutoff: (wallet: string, reason: string) =>
    request<{ ok: boolean }>("/api/cutoff", {
      method: "POST",
      body: JSON.stringify({ wallet, reason }),
    }),
  clearCutoff: (wallet: string) =>
    request<{ ok: boolean }>(`/api/cutoff/${encodeURIComponent(wallet)}`, { method: "DELETE" }),
  executionMode: () => request<ExecutionMode>("/api/execution_mode"),
  setExecutionMode: (mode: "paper" | "live") =>
    request<ExecutionMode>("/api/execution_mode", {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  clearExecutionMode: () =>
    request<ExecutionMode>("/api/execution_mode", { method: "DELETE" }),
};

export interface ExecutionMode {
  effective: "paper" | "live";
  override: "paper" | "live" | null;
  config_allows_live: boolean;
}

export interface Summary {
  bankroll_usdc: number;
  realized_pnl_usdc: number;
  unrealized_pnl_usdc: number;
  equity_usdc: number;
  open_positions: number;
  open_exposure_usdc: number;
  daily_pnl_usdc: number;
  weekly_pnl_usdc: number;
  global_halt: { halted: boolean; reason: string | null; since: number | null };
  cutoff_count: number;
  dry_run: boolean | null;
  bot_config_path: string | null;
}

export interface EquityPoint { ts: number; equity: number; }

export interface Position {
  position_id: string;
  signal_id: string | null;
  source_wallet: string;
  market_id: string;
  token_id: string;
  outcome: string;
  side: string;
  entry_price: number;
  size: number;
  notional: number;
  opened_at: number;
  closed_at: number | null;
  exit_price: number | null;
  realized_pnl: number;
  status: string;
}

export interface Trader {
  wallet: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  realized_pnl: number;
  total_notional: number;
  roi: number;
  max_drawdown: number;
  consecutive_losses: number;
  score: number;
  cutoff: { reason: string; set_at: number } | null;
}

export interface DecisionsPage {
  items: { line_no: number; raw: Record<string, unknown> }[];
  next_offset: number;
  total_bytes: number;
}
