import { useEffect, useState } from "react";
import { getApiKey, setApiKey } from "../api/client";

interface Props {
  children: React.ReactNode;
}

export default function ApiKeyGate({ children }: Props) {
  const [hasKey, setHasKey] = useState<boolean>(() => !!getApiKey());
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!hasKey) return;
    fetch("/api/summary", { headers: { "X-API-Key": getApiKey() ?? "" } })
      .then((r) => {
        if (r.status === 401) {
          setHasKey(false);
          setErr("API key rejected by server");
        }
      })
      .catch(() => {});
  }, [hasKey]);

  if (hasKey) return <>{children}</>;

  return (
    <div className="gate">
      <h2 style={{ marginTop: 0 }}>Sign in</h2>
      <p className="muted" style={{ fontSize: 13 }}>
        Enter the dashboard API key (set as <span className="mono">DASHBOARD_API_KEY</span> on the server).
      </p>
      <input
        type="password"
        autoFocus
        placeholder="API key"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && draft.trim()) {
            setApiKey(draft.trim());
            setErr(null);
            setHasKey(true);
          }
        }}
      />
      {err && <div className="banner bad" style={{ marginTop: 12 }}>{err}</div>}
      <button
        className="primary"
        style={{ marginTop: 12, width: "100%" }}
        disabled={!draft.trim()}
        onClick={() => {
          setApiKey(draft.trim());
          setErr(null);
          setHasKey(true);
        }}
      >
        Continue
      </button>
    </div>
  );
}
