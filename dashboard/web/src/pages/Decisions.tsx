import { useEffect, useRef, useState } from "react";
import { api, type DecisionsPage } from "../api/client";

// Event names emitted by bot/core/orchestrator.py and enhancements.py.
// "" means "all" in the dropdown.
const TYPES = [
  "",
  "copied",
  "rejected",
  "exit",
  "exit_failed",
  "signal_cluster",
  "adverse_selection_check",
  "stuck_signal_recovered",
  "trader_sell_observed",
];

export default function Decisions() {
  const [type, setType] = useState("");
  const [items, setItems] = useState<DecisionsPage["items"]>([]);
  const [error, setError] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const offsetRef = useRef(0);
  const seqRef = useRef(0);
  const tailRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setItems([]);
    offsetRef.current = 0;
    seqRef.current = 0;
  }, [type]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      try {
        const page = await api.decisions(offsetRef.current, type || undefined);
        if (cancelled) return;
        offsetRef.current = page.next_offset;
        // Tag items with a monotonic id so React keys stay stable across slices.
        const stamped = page.items.map((it) => ({
          ...it,
          _id: ++seqRef.current,
        }));
        setItems((prev) => [...prev, ...stamped].slice(-2000));
        setError(null);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) timer = setTimeout(tick, 3000);
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [type]);

  useEffect(() => {
    if (autoScroll) tailRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, autoScroll]);

  return (
    <>
      <h1 style={{ marginTop: 0 }}>Decisions</h1>
      <div className="row" style={{ marginBottom: 16 }}>
        <label>
          Type{" "}
          <select value={type} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((t) => <option key={t} value={t}>{t || "(all)"}</option>)}
          </select>
        </label>
        <label>
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
          {" "}auto-scroll
        </label>
        <span className="muted" style={{ fontSize: 12 }}>{items.length} events shown</span>
      </div>

      {error && <div className="banner bad">Failed: {error}</div>}

      <div className="panel mono" style={{ maxHeight: "60vh", overflow: "auto", fontSize: 12 }}>
        {items.length === 0 && <div className="muted">No events yet.</div>}
        {items.map((it) => {
          const ev = (it.raw as { event?: string }).event ?? "?";
          const id = (it as unknown as { _id: number })._id;
          return (
            <div key={id} style={{ padding: "4px 0", borderBottom: "1px solid #1b2230" }}>
              <span className={`tag ${tagTone(ev)}`}>{ev}</span>{" "}
              <span>{JSON.stringify(it.raw)}</span>
            </div>
          );
        })}
        <div ref={tailRef} />
      </div>
    </>
  );
}

function tagTone(ev: string): string {
  if (ev === "copied") return "good";
  if (ev === "rejected" || ev === "exit_failed") return "bad";
  if (ev === "exit" || ev === "signal_cluster" || ev === "adverse_selection_check") return "warn";
  return "muted";
}
