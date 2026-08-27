import { useEffect, useState } from "react";
import { api } from "../api";

// Right-side leaderboard box (feature 3): top 10 agents by average score
// in the current date range, by default. Clicking a row opens that agent's
// full scorecard (parameter breakdown + strengths/improvements).
//
// Search box (new): typing searches ALL agents (not just the top 10) via the
// same /api/dashboard/top-agents endpoint with a `search` param, so there's
// no separate search API to keep in sync. Clearing the box reverts to
// exactly the `agents` prop passed down from Dashboard.jsx (the original
// top-10, fetched once on page load) - existing behavior untouched.
export default function TopAgentsBox({ agents, start, end, onSelectAgent }) {
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const term = query.trim();
    if (!term) {
      setSearchResults(null);
      setError("");
      return;
    }
    setSearching(true);
    const timer = setTimeout(() => {
      api
        .topAgents(start, end, 25, term)
        .then((data) => setSearchResults(data.agents))
        .catch((err) => setError(err.message || "Search failed"))
        .finally(() => setSearching(false));
    }, 300); // debounce - don't fire a request on every single keystroke
    return () => clearTimeout(timer);
  }, [query, start, end]);

  const isSearchActive = query.trim().length > 0;
  const displayed = isSearchActive ? (searchResults || []) : agents;

  return (
    <aside className="leaderboard-box">
      <div className="leaderboard-head">
        <h2>Top agents</h2>
        {/* <span className="hint">{isSearchActive ? "search results" : "by average score"}</span> */}
      </div>

      {/* <div className="leaderboard-search">
        <input
          type="text"
          placeholder="Search any agent…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {isSearchActive && (
          <button type="button" className="leaderboard-search-clear" onClick={() => setQuery("")} title="Clear search">
            ×
          </button>
        )}
      </div> */}

      {error && <div className="login-error" style={{ margin: "0 12px 8px" }}>{error}</div>}

      {isSearchActive && searching && (
        <div className="state-msg" style={{ padding: "16px 12px" }}>Searching…</div>
      )}

      {!searching && displayed.length === 0 && (
        <div className="state-msg" style={{ padding: "24px 12px" }}>
          {isSearchActive ? "No agents match that search." : "Not enough audited calls yet."}
        </div>
      )}

      {!searching && (
        <ol className="leaderboard-list">
          {displayed.map((a, i) => (
            <li key={a.agent_id}>
              <button
                type="button"
                className="leaderboard-row"
                onClick={() => onSelectAgent(a.agent_id)}
              >
                <span className={`leaderboard-rank ${!isSearchActive && i < 3 ? "top3" : ""}`}>
                  {isSearchActive ? "•" : i + 1}
                </span>
                <span className="leaderboard-info">
                  <span className="leaderboard-name">{a.agent_name}</span>
                  <span className="leaderboard-team">{a.team || "—"} · {a.call_count} calls</span>
                </span>
                <span className="leaderboard-score mono">{a.avg_score ?? "—"}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </aside>
  );
}