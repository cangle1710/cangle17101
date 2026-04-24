import { useState } from "react";
import { api } from "../api/client";
import { usePolling } from "../components/usePolling";

type Sort = "score" | "roi" | "pnl" | "trades";

export default function Traders() {
  const [sort, setSort] = useState<Sort>("score");
  const { data, error, loading } = usePolling(() => api.traders(sort), 5000, [sort]);
  const [busy, setBusy] = useState<string | null>(null);

  async function toggleCutoff(wallet: string, isCut: boolean) {
    setBusy(wallet);
    try {
      if (isCut) await api.clearCutoff(wallet);
      else {
        const reason = prompt(`Cutoff reason for ${wallet}?`);
        if (!reason) return;
        await api.setCutoff(wallet, reason);
      }
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <h1 style={{ marginTop: 0 }}>Traders</h1>
      <div className="row" style={{ marginBottom: 16 }}>
        <label>
          Sort{" "}
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="score">score</option>
            <option value="roi">ROI</option>
            <option value="pnl">P&L</option>
            <option value="trades">trades</option>
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
                <th>wallet</th>
                <th style={{ textAlign: "right" }}>score</th>
                <th style={{ textAlign: "right" }}>trades</th>
                <th style={{ textAlign: "right" }}>WR</th>
                <th style={{ textAlign: "right" }}>ROI</th>
                <th style={{ textAlign: "right" }}>P&L</th>
                <th style={{ textAlign: "right" }}>DD</th>
                <th style={{ textAlign: "right" }}>streak</th>
                <th>cutoff</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.length === 0 && (
                <tr><td colSpan={10} className="muted" style={{ padding: 24, textAlign: "center" }}>No traders tracked yet.</td></tr>
              )}
              {data.map((t) => {
                const isCut = !!t.cutoff;
                return (
                  <tr key={t.wallet}>
                    <td className="mono">{t.wallet}</td>
                    <td style={{ textAlign: "right" }}>{t.score.toFixed(2)}</td>
                    <td style={{ textAlign: "right" }}>{t.trades}</td>
                    <td style={{ textAlign: "right" }}>{(t.win_rate * 100).toFixed(1)}%</td>
                    <td style={{ textAlign: "right" }}>{(t.roi * 100).toFixed(2)}%</td>
                    <td style={{ textAlign: "right" }}>{t.realized_pnl.toFixed(2)}</td>
                    <td style={{ textAlign: "right" }}>{(t.max_drawdown * 100).toFixed(1)}%</td>
                    <td style={{ textAlign: "right" }}>{t.consecutive_losses}</td>
                    <td>
                      {isCut ? (
                        <span className="tag bad" title={t.cutoff!.reason}>cut: {t.cutoff!.reason}</span>
                      ) : (
                        <span className="tag muted">—</span>
                      )}
                    </td>
                    <td>
                      <button
                        disabled={busy === t.wallet}
                        className={isCut ? "" : "danger"}
                        onClick={() => toggleCutoff(t.wallet, isCut)}
                      >
                        {isCut ? "uncutoff" : "cutoff"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
