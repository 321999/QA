import { useEffect, useState } from "react";
import { api } from "../api";

function chipClass(score) {
  if (score >= 80) return "good";
  if (score >= 60) return "avg";
  return "poor";
}

export default function ParameterAgents({ parameter, start, end, onBack, onSelectAgent }) {
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true);
    api
      .agentsForParameter(parameter, start, end)
      .then((data) => setAgents(data.agents))
      .catch((err) => setError(err.message || "Could not load agents"))
      .finally(() => setLoading(false));
  }, [parameter, start, end]);

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">Overview</button>
        <span>/</span>
        <span>{parameter}</span>
      </div>
      <h1 className="page-title">{parameter}</h1>
      <p className="page-sub">Agents ranked by average score on this parameter, {start} to {end}.</p>

      {error && <div className="login-error">{error}</div>}

      {loading ? (
        <div className="state-msg">Loading agents…</div>
      ) : (
        <div className="agent-table">
          {agents.map((a) => (
            <button
              key={a.agent_id}
              className="agent-row"
              onClick={() => onSelectAgent(a.agent_id, start, end)}
              type="button"
            >
              <span className="name">{a.agent_name}</span>
              <span className="team">{a.team}</span>
              <span className="calls">{a.calls_scored} calls scored</span>
              <span className={`score-chip ${chipClass(a.avg_score)}`}>{a.avg_score}</span>
            </button>
          ))}
          {agents.length === 0 && (
            <div className="state-msg">No agents were scored on this parameter in this range.</div>
          )}
        </div>
      )}
    </div>
  );
}
