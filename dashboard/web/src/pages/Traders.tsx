import { useState } from "react";
import { api } from "../api/client";
import ReasonModal from "../components/ReasonModal";
import { usePolling } from "../components/usePolling";

type Sort = "score" | "roi" | "pnl" | "trades";

export default function Traders() {
  const [sort, setSort] = useState<Sort>("score");
  const { data, error, loading } = usePolling(() => api.traders(sort), 5000, [sort]);
  const [busy, setBusy] = useState<string | null>(null);
  const [cuttingWallet, setCuttingWallet] = useState<string | null>(null);

  async function clearCutoff(wallet: string) {
    setBusy(wallet);
    try {
      await api.clearCutoff(wallet);
    } finally {
      setBusy(null);
    }
  }

  async function setCutoff(wallet: string, reason: string) {
    setBusy(wallet);
    setCuttingWallet(null);
    try {
      await api.setCutoff(wallet, reason);
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
                        onClick={() => isCut ? clearCutoff(t.wallet) : setCuttingWallet(t.wallet)}
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

      <ReasonModal
        open={!!cuttingWallet}
        title={cuttingWallet ? `Cut off ${cuttingWallet}` : ""}
        placeholder="reason (e.g. 5 consec losses)"
        confirmLabel="Cutoff"
        onCancel={() => setCuttingWallet(null)}
        onConfirm={(reason) => cuttingWallet && setCutoff(cuttingWallet, reason)}
      />
    </>
  );
}
