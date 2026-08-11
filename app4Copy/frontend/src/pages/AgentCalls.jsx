import { useEffect, useState } from "react";
import { api } from "../api";

function chipClass(score) {
  if (score == null) return "avg";
  if (score >= 80) return "good";
  if (score >= 60) return "avg";
  return "poor";
}

export default function AgentCalls({ category, label, agentId, agentName, start, end, onBack, onSelectCall }) {
  const [calls, setCalls] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true);
    api
      .categoryAgentCalls(category, agentId, start, end)
      .then((data) => setCalls(data.calls))
      .catch((err) => setError(err.message || "Could not load calls"))
      .finally(() => setLoading(false));
  }, [category, agentId, start, end]);

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">{label}</button>
        <span>/</span>
        <span>{agentName}</span>
      </div>
      <h1 className="page-title">{agentName}</h1>
      <p className="page-sub">{label} for this agent, {start} to {end} — tap a call to hear why it scored the way it did.</p>

      {error && <div className="login-error">{error}</div>}

      {loading ? (
        <div className="state-msg">Loading calls…</div>
      ) : (
        <div className="agent-table">
          {calls.map((c) => (
            <button
              key={c.id}
              className="agent-row"
              type="button"
              onClick={() => onSelectCall(c.id)}
              disabled={c.status !== "audited"}
              style={{ opacity: c.status === "audited" ? 1 : 0.55 }}
            >
              <span className="name">
                {c.call_start_time || c.call_date}
                <span style={{ color: "var(--gray)", fontWeight: 400 }}> · {c.call_number || c.recording_base || "—"}</span>
              </span>
              <span className="team">{c.verdict || c.status}</span>
              <span className="calls">{c.sentiment || "—"}</span>
              <span className={`score-chip ${chipClass(c.overall_score)}`}>{c.overall_score ?? "—"}</span>
            </button>
          ))}
          {calls.length === 0 && (
            <div className="state-msg">No calls for this agent in this category/range.</div>
          )}
        </div>
      )}
    </div>
  );
}
