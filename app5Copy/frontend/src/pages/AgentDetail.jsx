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

// "Total Calls" and "Average Call" both draw from the exact same audited
// population agent_detail()'s stats are computed over (see main.py's
// CATEGORY_FILTERS - both map to the "audited" filter), only the label
// shown on the next page differs. "Good"/"Poor" map to their own filters.
const CARD_DEFS = [
  { key: "total", label: "Total Calls", field: "total_calls", accent: "ink", category: "audited" },
  { key: "average", label: "Average Call", field: "average_call_score", accent: "teal", category: "audited" },
  { key: "good", label: "Good Calls", field: "good_calls", accent: "teal", category: "good" },
  { key: "poor", label: "Poor Calls", field: "poor_calls", accent: "coral", category: "poor" },
];

export default function AgentDetail({ agentId, start, end, onBack, onSelectCategory }) {
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

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">Overview</button>
        <span>/</span>
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
        {CARD_DEFS.map((c) => (
          <button
            key={c.key}
            type="button"
            className={`stat-card accent-${c.accent} clickable`}
            onClick={() => onSelectCategory(c.category, c.label, data.agent.name)}
            title={`See the calls behind ${c.label.toLowerCase()}`}
          >
            <div className="label">{c.label}</div>
            <div className="value mono">{data[c.field]}</div>
          </button>
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