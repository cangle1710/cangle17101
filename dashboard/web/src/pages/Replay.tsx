import { useState } from "react";
import { api, type ReplayResult } from "../api/client";

export default function Replay() {
  const [file, setFile] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ReplayResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    setBusy(true); setErr(null);
    try {
      const r = await api.replay(file.trim() || undefined);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1 style={{ marginTop: 0 }}>Replay</h1>
      <div className="panel">
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Mirrors <span className="mono">python -m bot.cli replay</span>:
          summarises the bot's decisions journal (event counts and reject-reason
          histogram). Leave the path blank to use the bot's configured
          <span className="mono"> logging.decisions_file</span>.
        </p>
        <div className="row">
          <input
            placeholder="(default: bot's configured decisions file)"
            value={file}
            onChange={(e) => setFile(e.target.value)}
            style={{ flex: 1, minWidth: 320 }}
          />
          <button className="primary" disabled={busy} onClick={run}>Run replay</button>
        </div>
      </div>

      {err && <div className="banner bad">{err}</div>}

      {result && (
        <>
          <div className="panel">
            <h2>{result.total_events} events</h2>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              <span className="mono">{result.file}</span>
            </p>
          </div>
          <div className="panel">
            <h2>Event counts</h2>
            <Histogram entries={result.counts} />
          </div>
          {Object.keys(result.reject_reasons).length > 0 && (
            <div className="panel">
              <h2>Reject reasons</h2>
              <Histogram entries={result.reject_reasons} />
            </div>
          )}
        </>
      )}
    </>
  );
}

function Histogram({ entries }: { entries: Record<string, number> }) {
  const sorted = Object.entries(entries).sort((a, b) => b[1] - a[1]);
  const max = sorted.reduce((m, [, n]) => Math.max(m, n), 1);
  return (
    <div>
      {sorted.map(([k, n]) => (
        <div key={k} style={{ display: "flex", alignItems: "center", marginBottom: 4 }}>
          <div className="mono" style={{ width: 200, fontSize: 12 }}>{k}</div>
          <div style={{
            background: "var(--accent)",
            width: `${(n / max) * 60}%`,
            height: 14,
            marginRight: 8,
            borderRadius: 2,
          }} />
          <div className="mono" style={{ fontSize: 12 }}>{n}</div>
        </div>
      ))}
    </div>
  );
}
