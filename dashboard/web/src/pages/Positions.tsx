import { useState } from "react";
import { api } from "../api/client";
import { usePolling } from "../components/usePolling";

export default function Positions() {
  const [statusFilter, setStatusFilter] = useState<"open" | "closed" | "all">("open");
  const { data, error, loading } = usePolling(
    () => api.positions(statusFilter),
    5000,
    [statusFilter],
  );

  return (
    <>
      <h1 style={{ marginTop: 0 }}>Positions</h1>
      <div className="row" style={{ marginBottom: 16 }}>
        <label>
          Status{" "}
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value as any)}>
            <option value="open">open</option>
            <option value="closed">closed</option>
            <option value="all">all</option>
          </select>
        </label>
      </div>

      {error && <div className="banner bad">Failed: {error.message}</div>}
      {loading && !data && <div>Loading…</div>}

      {data && (
        <div className="panel" style={{ padding: 0, overflow: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>opened</th>
                <th>wallet</th>
                <th>token</th>
                <th>side</th>
                <th>outcome</th>
                <th style={{ textAlign: "right" }}>entry</th>
                <th style={{ textAlign: "right" }}>size</th>
                <th style={{ textAlign: "right" }}>notional</th>
                <th style={{ textAlign: "right" }}>realized</th>
                <th>status</th>
              </tr>
            </thead>
            <tbody>
              {data.length === 0 && (
                <tr><td colSpan={10} className="muted" style={{ padding: 24, textAlign: "center" }}>No positions.</td></tr>
              )}
              {data.map((p) => (
                <tr key={p.position_id}>
                  <td className="mono">{new Date(p.opened_at * 1000).toLocaleString()}</td>
                  <td className="mono">{p.source_wallet.slice(0, 10)}…</td>
                  <td className="mono">{p.token_id.slice(0, 10)}…</td>
                  <td>{p.side}</td>
                  <td>{p.outcome}</td>
                  <td style={{ textAlign: "right" }}>{p.entry_price.toFixed(4)}</td>
                  <td style={{ textAlign: "right" }}>{p.size.toFixed(2)}</td>
                  <td style={{ textAlign: "right" }}>{p.notional.toFixed(2)}</td>
                  <td style={{ textAlign: "right" }}>{p.realized_pnl.toFixed(2)}</td>
                  <td>
                    <span className={`tag ${p.status === "OPEN" ? "good" : "muted"}`}>{p.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
