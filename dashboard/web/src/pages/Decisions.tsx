import { useEffect, useRef, useState } from "react";
import { api, type DecisionsPage } from "../api/client";

const TYPES = ["", "copy", "rejected", "exit", "sized"];

export default function Decisions() {
  const [type, setType] = useState("");
  const [items, setItems] = useState<DecisionsPage["items"]>([]);
  const [error, setError] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const offsetRef = useRef(0);
  const tailRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setItems([]);
    offsetRef.current = 0;
  }, [type]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      try {
        const page = await api.decisions(offsetRef.current, type || undefined);
        if (cancelled) return;
        offsetRef.current = page.next_offset;
        setItems((prev) => [...prev.slice(-1000), ...page.items].slice(-2000));
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
        {items.map((it, i) => {
          const ev = (it.raw as any).event ?? "?";
          return (
            <div key={`${i}-${it.line_no}`} style={{ padding: "4px 0", borderBottom: "1px solid #1b2230" }}>
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
  if (ev === "copy" || ev === "filled") return "good";
  if (ev === "rejected" || ev === "aborted") return "bad";
  if (ev === "exit") return "warn";
  return "muted";
}
