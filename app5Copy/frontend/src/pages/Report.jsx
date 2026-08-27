import { useEffect, useState, useCallback } from "react";
import { api } from "../api";

const SORT_OPTIONS = [
  { value: "call_date_desc", label: "Newest first" },
  { value: "call_date_asc", label: "Oldest first" },
  { value: "score_desc", label: "Highest score" },
  { value: "score_asc", label: "Lowest score" },
  { value: "agent_name", label: "Agent name" },
];

function scoreClass(score) {
  if (score === null || score === undefined) return "";
  if (score >= 80) return "good";
  if (score >= 60) return "avg";
  return "poor";
}

export default function Reports({ start, end, onSelectCall }) {
  const [agents, setAgents] = useState([]);
  const [agentId, setAgentId] = useState("");
  const [sort, setSort] = useState("call_date_desc");
  const [page, setPage] = useState(1);
  const pageSize = 25;

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    api.agents().then((d) => setAgents(d.agents)).catch(() => {});
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.agentScoresReport({ agentId: agentId || undefined, start, end, page, pageSize, sort });
      setData(res);
    } catch (err) {
      setError(err.message || "Could not load report");
    } finally {
      setLoading(false);
    }
  }, [agentId, start, end, page, sort]);

  useEffect(() => { load(); }, [load]);

  // Any filter change should snap back to page 1 - a stale page number from
  // a previous filter can point past the end of the new result set.
  function updateAgentFilter(v) { setAgentId(v); setPage(1); }
  function updateSort(v) { setSort(v); setPage(1); }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;

  return (
    <div>
      <h1 className="page-title">Agent report</h1>
      <p className="page-sub">Every audited call, broken down by agent, recording, and parameter score.</p>

      <div className="range-row">
        <div className="quick-ranges" style={{ gap: 10 }}>
          <select value={agentId} onChange={(e) => updateAgentFilter(e.target.value)} className="report-select">
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name}{a.team ? ` · ${a.team}` : ""}</option>
            ))}
          </select>
          <select value={sort} onChange={(e) => updateSort(e.target.value)} className="report-select">
            {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        {data && (
          <span className="hint">{data.total} audited call{data.total === 1 ? "" : "s"} in range</span>
        )}
      </div>

      {error && <div className="login-error">{error}</div>}

      {loading ? (
        <div className="state-msg">Loading report…</div>
      ) : (
        <>
          <div className="report-table-wrap">
            <table className="report-table">
              <thead>
                <tr>
                  <th className="sticky-col">Agent</th>
                  <th className="sticky-col-2">Recording</th>
                  <th>Overall</th>
                  {data?.columns.map((c) => <th key={c.key} title={c.label}>{c.label}</th>)}
                </tr>
              </thead>
              <tbody>
                {data?.rows.map((row) => (
                  <tr key={row.call_id} onClick={() => onSelectCall(row.call_id)}>
                    <td className="sticky-col report-agent-cell">{row.agent_name}</td>
                    <td className="sticky-col-2 mono report-recording-cell">{row.recording_base || "—"}</td>
                    <td>
                      <span className={`score-chip ${scoreClass(row.overall_score)}`}>
                        {row.overall_score ?? "—"}
                      </span>
                    </td>
                    {data.columns.map((c) => (
                      <td key={c.key} className="mono report-score-cell">
                        {row[c.key] ?? "—"}
                      </td>
                    ))}
                  </tr>
                ))}
                {data?.rows.length === 0 && (
                  <tr><td colSpan={data.columns.length + 3} className="state-msg">No audited calls match these filters.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="report-pagination">
            <button type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</button>
            <span className="mono">Page {page} of {totalPages}</span>
            <button type="button" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next</button>
          </div>
        </>
      )}
    </div>
  );
}