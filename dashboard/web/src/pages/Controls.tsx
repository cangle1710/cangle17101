import { useEffect, useState } from "react";
import { api, type ExecutionMode } from "../api/client";
import { usePolling } from "../components/usePolling";

export default function Controls() {
  const summary = usePolling(() => api.summary(), 5000);
  const [haltReason, setHaltReason] = useState("");
  const [cutWallet, setCutWallet] = useState("");
  const [cutReason, setCutReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<ExecutionMode | null>(null);

  useEffect(() => {
    api.executionMode().then(setMode).catch(() => {});
  }, []);

  async function refreshMode() {
    try {
      setMode(await api.executionMode());
    } catch {}
  }

  async function doSetHalt() {
    if (!haltReason.trim()) return;
    setBusy(true); setErr(null); setMsg(null);
    try { await api.setHalt(haltReason.trim()); setMsg(`halt set: ${haltReason}`); setHaltReason(""); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  async function doClearHalt() {
    setBusy(true); setErr(null); setMsg(null);
    try { await api.clearHalt(); setMsg("halt cleared"); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  async function doCutoff() {
    if (!cutWallet.trim() || !cutReason.trim()) return;
    setBusy(true); setErr(null); setMsg(null);
    try {
      await api.setCutoff(cutWallet.trim(), cutReason.trim());
      setMsg(`cutoff set for ${cutWallet}`);
      setCutWallet(""); setCutReason("");
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  async function doUncutoff() {
    if (!cutWallet.trim()) return;
    setBusy(true); setErr(null); setMsg(null);
    try { await api.clearCutoff(cutWallet.trim()); setMsg(`cutoff cleared for ${cutWallet}`); setCutWallet(""); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  async function doSetMode(target: "paper" | "live") {
    if (target === "live") {
      const ok = window.confirm(
        "Switch to LIVE trading? Real orders will be signed and submitted on the next signal. Continue?",
      );
      if (!ok) return;
    }
    setBusy(true); setErr(null); setMsg(null);
    try {
      const next = await api.setExecutionMode(target);
      setMode(next);
      setMsg(`execution mode = ${next.effective}`);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  async function doClearMode() {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const next = await api.clearExecutionMode();
      setMode(next);
      setMsg("override cleared; following YAML");
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  const halt = summary.data?.global_halt;

  return (
    <>
      <h1 style={{ marginTop: 0 }}>Controls</h1>

      {msg && <div className="banner good">{msg}</div>}
      {err && <div className="banner bad">{err}</div>}

      <div className="panel">
        <h2>Execution mode</h2>
        {mode ? (
          <>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Currently <strong>{mode.effective.toUpperCase()}</strong>
              {mode.override && (
                <span className="muted"> · override: {mode.override}</span>
              )}
              {!mode.config_allows_live && (
                <span className="muted">
                  {" "}· YAML pinned <span className="mono">execution.dry_run=true</span>;
                  flip in <span className="mono">bot/config.yaml</span> and restart to enable live.
                </span>
              )}
              {" "}Bot picks up changes within ~60 s.
            </p>
            <div className="row">
              <button
                disabled={busy || mode.effective === "paper"}
                onClick={() => doSetMode("paper")}
              >
                Force paper
              </button>
              <button
                className="danger"
                disabled={busy || !mode.config_allows_live || mode.effective === "live"}
                onClick={() => doSetMode("live")}
                title={!mode.config_allows_live ? "Disabled by YAML config" : undefined}
              >
                Switch to live
              </button>
              <button
                disabled={busy || mode.override === null}
                onClick={doClearMode}
              >
                Clear override
              </button>
              <button onClick={refreshMode} style={{ marginLeft: "auto" }}>Refresh</button>
            </div>
          </>
        ) : (
          <div className="muted">loading…</div>
        )}
      </div>

      <div className="panel">
        <h2>Global halt</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          {halt?.halted
            ? <>Currently halted — reason: <strong>{halt.reason}</strong></>
            : <>Bot is running.</>}
          {" "}Bot picks up changes within ~60 s on the maintenance tick.
        </p>
        <div className="row">
          <input
            placeholder="reason (e.g. 'ops maintenance')"
            value={haltReason}
            onChange={(e) => setHaltReason(e.target.value)}
            style={{ flex: 1, minWidth: 240 }}
          />
          <button className="danger" disabled={busy || !haltReason.trim()} onClick={doSetHalt}>Halt</button>
          <button disabled={busy || !halt?.halted} onClick={doClearHalt}>Resume</button>
        </div>
      </div>

      <div className="panel">
        <h2>Trader cutoff</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          A cutoff blocks any future signals from the wallet. Existing open positions are unaffected.
        </p>
        <div className="row">
          <input
            placeholder="0xwallet…"
            value={cutWallet}
            onChange={(e) => setCutWallet(e.target.value)}
            style={{ minWidth: 360 }}
          />
          <input
            placeholder="reason"
            value={cutReason}
            onChange={(e) => setCutReason(e.target.value)}
            style={{ flex: 1, minWidth: 200 }}
          />
          <button className="danger" disabled={busy || !cutWallet.trim() || !cutReason.trim()} onClick={doCutoff}>Cutoff</button>
          <button disabled={busy || !cutWallet.trim()} onClick={doUncutoff}>Uncutoff</button>
        </div>
      </div>
    </>
  );
}
