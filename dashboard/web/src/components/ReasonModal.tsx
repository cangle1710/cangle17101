import { useEffect, useRef, useState } from "react";

interface Props {
  title: string;
  placeholder?: string;
  confirmLabel?: string;
  open: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}

export default function ReasonModal({
  title,
  placeholder = "reason",
  confirmLabel = "Confirm",
  open,
  onCancel,
  onConfirm,
}: Props) {
  const [reason, setReason] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setReason("");
      // Defer focus to next tick so the input is mounted.
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  if (!open) return null;

  const submit = () => {
    if (!reason.trim()) return;
    onConfirm(reason.trim());
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
      onClick={onCancel}
    >
      <div
        className="panel"
        style={{ width: 420, maxWidth: "90vw" }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 style={{ marginTop: 0 }}>{title}</h2>
        <input
          ref={inputRef}
          placeholder={placeholder}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") onCancel();
          }}
          style={{ width: "100%" }}
        />
        <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <button onClick={onCancel}>Cancel</button>
          <button className="danger" disabled={!reason.trim()} onClick={submit}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
