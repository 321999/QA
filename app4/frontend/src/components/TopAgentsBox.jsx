// Right-side leaderboard box (feature 3): top 10 agents by average score
// in the current date range. Clicking a row opens that agent's full
// scorecard (parameter breakdown + strengths/improvements).
export default function TopAgentsBox({ agents, onSelectAgent }) {
  return (
    <aside className="leaderboard-box">
      <div className="leaderboard-head">
        <h2>Top agents</h2>
        <span className="hint">by average score</span>
      </div>

      {agents.length === 0 && (
        <div className="state-msg" style={{ padding: "24px 12px" }}>
          Not enough audited calls yet.
        </div>
      )}

      <ol className="leaderboard-list">
        {agents.map((a, i) => (
          <li key={a.agent_id}>
            <button
              type="button"
              className="leaderboard-row"
              onClick={() => onSelectAgent(a.agent_id)}
            >
              <span className={`leaderboard-rank ${i < 3 ? "top3" : ""}`}>{i + 1}</span>
              <span className="leaderboard-info">
                <span className="leaderboard-name">{a.agent_name}</span>
                <span className="leaderboard-team">{a.team || "—"} · {a.call_count} calls</span>
              </span>
              <span className="leaderboard-score mono">{a.avg_score ?? "—"}</span>
            </button>
          </li>
        ))}
      </ol>
    </aside>
  );
}
