import { useState } from "react";
import { getApiKey, setApiKey } from "../api/client";

interface Props {
  children: React.ReactNode;
}

export default function ApiKeyGate({ children }: Props) {
  // The first authenticated API call validates the key — if the server
  // returns 401, the fetch wrapper clears localStorage and the next render
  // will fall back to this gate. No upfront probe needed.
  const [hasKey, setHasKey] = useState<boolean>(() => !!getApiKey());
  const [draft, setDraft] = useState("");

  if (hasKey) return <>{children}</>;

  const submit = () => {
    if (!draft.trim()) return;
    setApiKey(draft.trim());
    setHasKey(true);
  };

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
          if (e.key === "Enter") submit();
        }}
      />
      <button
        className="primary"
        style={{ marginTop: 12, width: "100%" }}
        disabled={!draft.trim()}
        onClick={submit}
      >
        Continue
      </button>
    </div>
  );
}
