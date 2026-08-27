import { useEffect, useState } from "react";
import { api } from "../api";

function chipClass(score) {
  if (score == null) return "avg";
  if (score >= 80) return "good";
  if (score >= 60) return "avg";
  return "poor";
}

export default function CategoryAgents({ category, label, start, end, onBack, onSelectAgent }) {
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true);
    api
      .categoryAgents(category, start, end)
      .then((data) => setAgents(data.agents))
      .catch((err) => setError(err.message || "Could not load agents"))
      .finally(() => setLoading(false));
  }, [category, start, end]);

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">Overview</button>
        <span>/</span>
        <span>{label}</span>
      </div>
      <h1 className="page-title">{label}</h1>
      <p className="page-sub">Agents ranked by average score on this call set, {start} to {end}.</p>

      {error && <div className="login-error">{error}</div>}

      {loading ? (
        <div className="state-msg">Loading agents…</div>
      ) : (
        <div className="agent-table">
          {agents.map((a) => (
            <button
              key={a.agent_id}
              className="agent-row"
              onClick={() => onSelectAgent(a.agent_id, a.agent_name)}
              type="button"
            >
              <span className="name">{a.agent_name}</span>
              <span className="team">{a.team}</span>
              <span className="calls">{a.call_count} calls</span>
              <span className={`score-chip ${chipClass(a.avg_score)}`}>{a.avg_score ?? "—"}</span>
            </button>
          ))}
          {agents.length === 0 && (
            <div className="state-msg">No agents have calls in this category for this range.</div>
          )}
        </div>
      )}
    </div>
  );
}
