import { api } from "../api/client";
import EquityChart from "../components/EquityChart";
import { usePolling } from "../components/usePolling";

const fmt = (n: number) =>
  n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export default function Overview() {
  const summary = usePolling(() => api.summary(), 5000);
  const series = usePolling(() => api.equitySeries(0), 10000);
  const mode = usePolling(() => api.executionMode(), 5000);

  if (summary.loading && !summary.data) return <div>Loading…</div>;
  if (summary.error) return <div className="banner bad">Failed to load summary: {summary.error.message}</div>;
  const s = summary.data!;

  return (
    <>
      <h1 style={{ marginTop: 0 }}>Overview</h1>

      {s.global_halt.halted ? (
        <div className="banner bad">
          <strong>HALTED</strong> — {s.global_halt.reason ?? "(no reason)"}
          {s.global_halt.since && (
            <span className="muted"> · since {new Date(s.global_halt.since * 1000).toLocaleString()}</span>
          )}
        </div>
      ) : (
        <div className="banner good">Running</div>
      )}

      {mode.data && (
        mode.data.effective === "live" ? (
          <div className="banner bad">
            <strong>LIVE TRADING</strong> — real orders are being signed and submitted
          </div>
        ) : (
          <div className="banner warn">
            <strong>PAPER TRADING</strong> — fills are simulated locally; no orders sent to Polymarket
            {mode.data.override === "paper" && mode.data.config_allows_live && (
              <span className="muted"> (operator override; YAML allows live)</span>
            )}
            {!mode.data.config_allows_live && (
              <span className="muted"> (config ceiling: execution.dry_run=true)</span>
            )}
          </div>
        )
      )}

      <div className="kpi-grid">
        <Kpi label="Equity (USDC)" value={fmt(s.equity_usdc)} />
        <Kpi label="Realized P&L" value={fmt(s.realized_pnl_usdc)} tone={pnlTone(s.realized_pnl_usdc)} />
        <Kpi label="Daily P&L" value={fmt(s.daily_pnl_usdc)} tone={pnlTone(s.daily_pnl_usdc)} />
        <Kpi label="Weekly P&L" value={fmt(s.weekly_pnl_usdc)} tone={pnlTone(s.weekly_pnl_usdc)} />
        <Kpi label="Open Positions" value={String(s.open_positions)} />
        <Kpi label="Open Exposure" value={fmt(s.open_exposure_usdc)} />
        <Kpi label="Trader Cutoffs" value={String(s.cutoff_count)} />
      </div>

      <div className="panel">
        <h2>Equity</h2>
        <EquityChart data={series.data ?? []} />
      </div>
    </>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: "good" | "bad" }) {
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className={`value${tone ? ` ${tone}` : ""}`}>{value}</div>
    </div>
  );
}

function pnlTone(n: number): "good" | "bad" | undefined {
  if (n > 0) return "good";
  if (n < 0) return "bad";
  return undefined;
}
