import { useEffect, useState } from "react";
import { api } from "../api/client";

type Cfg = Record<string, unknown>;

export default function Config() {
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    api.config().then(setCfg).catch((e: Error) => setErr(e.message));
  }, []);

  if (err) return <div className="banner bad">Failed: {err}</div>;
  if (!cfg) return <div>Loading…</div>;

  const path = String(cfg["_path"] ?? "");
  const mutable = (cfg["_runtime_mutable"] as string[]) ?? [];
  const sections = Object.entries(cfg).filter(([k]) => !k.startsWith("_"));

  const matchesFilter = (k: string, v: unknown): boolean => {
    if (!filter) return true;
    const needle = filter.toLowerCase();
    if (k.toLowerCase().includes(needle)) return true;
    return JSON.stringify(v).toLowerCase().includes(needle);
  };

  return (
    <>
      <h1 style={{ marginTop: 0 }}>Config</h1>
      <div className="banner warn">
        Read-only view of <span className="mono">{path || "bot/config.yaml"}</span>.
        Edit the file and restart the bot to change non-runtime values.
      </div>

      <div className="panel">
        <h2>Runtime-mutable</h2>
        <ul style={{ marginTop: 0 }}>
          {mutable.map((line, i) => (
            <li key={i} className="muted" style={{ fontSize: 13, marginBottom: 4 }}>{line}</li>
          ))}
        </ul>
      </div>

      <div className="panel">
        <input
          placeholder="filter… e.g. 'kelly', 'halt', 'demo'"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ width: "100%", marginBottom: 12 }}
        />
        {sections.map(([section, value]) => (
          <Section key={section} name={section} value={value} matchesFilter={matchesFilter} />
        ))}
      </div>
    </>
  );
}

function Section({
  name, value, matchesFilter,
}: {
  name: string;
  value: unknown;
  matchesFilter: (k: string, v: unknown) => boolean;
}) {
  if (!matchesFilter(name, value)) return null;
  return (
    <details open style={{ marginBottom: 12 }}>
      <summary style={{ cursor: "pointer", fontWeight: 600, padding: "6px 0" }}>
        {name}
      </summary>
      <pre className="mono" style={{
        background: "var(--panel-2)", padding: 12, borderRadius: 4,
        overflow: "auto", margin: 0, fontSize: 12,
      }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}
