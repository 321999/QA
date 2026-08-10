import { useEffect, useState } from "react";
import { api } from "../api";

function barColor(score) {
  if (score >= 80) return "var(--teal)";
  if (score >= 60) return "var(--amber)";
  return "var(--coral)";
}

function initials(name) {
  return name
    .split(" ")
    .map((n) => n[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

export default function AgentDetail({ agentId, start, end, onBack }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true);
    api
      .agentDetail(agentId, start, end)
      .then(setData)
      .catch((err) => setError(err.message || "Could not load agent"))
      .finally(() => setLoading(false));
  }, [agentId, start, end]);

  if (loading) return <div className="state-msg">Loading agent scorecard…</div>;
  if (error) return <div className="login-error">{error}</div>;
  if (!data) return null;

  const cards = [
    { label: "Total calls", value: data.total_calls, accent: "ink" },
    { label: "Average call score", value: data.average_call_score, accent: "teal" },
    { label: "Good calls", value: data.good_calls, accent: "teal" },
    { label: "Poor calls", value: data.poor_calls, accent: "coral" },
  ];

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">Overview</button>
        <span>/</span>
        <h5>this is where we need to make the changes</h5>
        <span>{data.agent.name}</span>
      </div>

      <div className="agent-header">
        <div className="agent-avatar">{initials(data.agent.name)}</div>
        <div>
          <h1 className="page-title" style={{ marginBottom: 2 }}>{data.agent.name}</h1>
          <p className="page-sub" style={{ marginBottom: 0 }}>{data.agent.team} · {start} to {end}</p>
        </div>
      </div>

      <div className="stat-grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
        {cards.map((c) => (
          <div key={c.label} className={`stat-card accent-${c.accent}`}>
            <div className="label">{c.label}</div>
            <div className="value mono">{c.value}</div>
          </div>
        ))}
      </div>

      <div className="section-head">
        <h2>Parameter-wise breakdown</h2>
      </div>
      <div className="param-list">
        {data.parameters.map((p, i) => (
          <div className="param-row" key={p.parameter}>
            <span className="idx">{String(i + 1).padStart(2, "0")}</span>
            <span className="name">{p.parameter}</span>
            <span className="bar-track">
              <span className="bar-fill" style={{ width: `${p.avg_score}%`, background: barColor(p.avg_score) }} />
            </span>
            <span className="score mono">{p.avg_score}</span>
          </div>
        ))}
        {data.parameters.length === 0 && (
          <div className="state-msg">No audited calls for this agent in this range.</div>
        )}
      </div>

      <div className="summary-grid">
        <div className="summary-card strength">
          <h3>Overall strengths</h3>
          <ul>
            {data.overall_summary.strengths.map((s) => <li key={s}>{s}</li>)}
            {data.overall_summary.strengths.length === 0 && <li>Not enough data yet</li>}
          </ul>
        </div>
        <div className="summary-card improve">
          <h3>Overall improvement areas</h3>
          <ul>
            {data.overall_summary.improvements.map((s) => <li key={s}>{s}</li>)}
            {data.overall_summary.improvements.length === 0 && <li>Not enough data yet</li>}
          </ul>
        </div>
      </div>
    </div>
  );
}
