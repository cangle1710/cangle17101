import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import type { EquityPoint } from "../api/client";

interface Props {
  data: EquityPoint[];
}

export default function EquityChart({ data }: Props) {
  if (!data.length) {
    return <div className="muted" style={{ padding: 24 }}>No equity data yet.</div>;
  }
  const formatted = data.map((p) => ({
    ts: new Date(p.ts * 1000).toLocaleString(),
    equity: p.equity,
  }));
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={formatted}>
        <XAxis dataKey="ts" tick={{ fill: "#8b95a5", fontSize: 11 }} hide />
        <YAxis tick={{ fill: "#8b95a5", fontSize: 11 }} domain={["auto", "auto"]} />
        <Tooltip
          contentStyle={{ background: "#161b22", border: "1px solid #2a3344", color: "#d7dde8" }}
          formatter={(v: number) => v.toFixed(2)}
        />
        <Line type="monotone" dataKey="equity" stroke="#4f8cff" strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
