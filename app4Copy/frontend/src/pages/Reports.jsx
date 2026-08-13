import { useEffect, useState, useCallback } from "react";
import { api } from "../api";

const SORT_OPTIONS = [
  { value: "call_date_desc", label: "Newest first" },
  { value: "call_date_asc", label: "Oldest first" },
  { value: "score_desc", label: "Highest score" },
  { value: "score_asc", label: "Lowest score" },
  { value: "agent_name", label: "Agent name" },
];

const DISPOSITION_STATUS_OPTIONS = [
  { value: "", label: "All predictions" },
  { value: "correct", label: "✅ Correct only" },
  { value: "incorrect", label: "❌ Incorrect only" },
];

function scoreClass(score) {
  if (score === null || score === undefined) return "";
  if (score >= 80) return "good";
  if (score >= 60) return "avg";
  return "poor";
}

// ✅/❌ badge for the Disposition Status column. null = nothing to compare
// (call missing either actual or predicted disposition) - shown as a plain
// dash rather than guessing at correct/incorrect.
function DispositionStatusBadge({ match }) {
  if (match === null || match === undefined) return <span style={{ color: "var(--gray)" }}>—</span>;
  return match
    ? <span className="score-chip good">✅ Correct</span>
    : <span className="score-chip poor">❌ Incorrect</span>;
}

export default function Reports({ start, end, onSelectCall }) {
  const [agents, setAgents] = useState([]);
  const [dispositionOptions, setDispositionOptions] = useState({ actual: [], predicted: [] });

  const [agentId, setAgentId] = useState("");
  const [dispositionStatus, setDispositionStatus] = useState("");
  const [actualDisposition, setActualDisposition] = useState("");
  const [predictedDisposition, setPredictedDisposition] = useState("");
  const [sort, setSort] = useState("call_date_desc");
  const [page, setPage] = useState(1);
  const pageSize = 25;

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    api.agents().then((d) => setAgents(d.agents)).catch(() => {});
  }, []);

  useEffect(() => {
    api.reportDispositionOptions(start, end).then(setDispositionOptions).catch(() => {});
  }, [start, end]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.agentScoresReport({
        agentId: agentId || undefined,
        dispositionStatus: dispositionStatus || undefined,
        actualDisposition: actualDisposition || undefined,
        predictedDisposition: predictedDisposition || undefined,
        start, end, page, pageSize, sort,
      });
      setData(res);
    } catch (err) {
      setError(err.message || "Could not load report");
    } finally {
      setLoading(false);
    }
  }, [agentId, dispositionStatus, actualDisposition, predictedDisposition, start, end, page, sort]);

  useEffect(() => { load(); }, [load]);

  // Any filter change should snap back to page 1 - a stale page number from
  // a previous filter can point past the end of the new result set.
  function updateFilter(setter) {
    return (v) => { setter(v); setPage(1); };
  }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;

  // The download link's filters are built from the exact same state as the
  // on-screen query (see api.js) - whatever's currently filtered is exactly
  // what downloads, page limit aside (export has no pagination, it's the
  // full filtered set, not just this one page).
  const downloadUrl = api.agentScoresReportExportUrl({
    agentId: agentId || undefined,
    dispositionStatus: dispositionStatus || undefined,
    actualDisposition: actualDisposition || undefined,
    predictedDisposition: predictedDisposition || undefined,
    start, end, sort,
  });

  return (
    <div>
      <h1 className="page-title">Agent report</h1>
      <p className="page-sub">Every audited call, broken down by agent, recording, parameter score, and disposition accuracy.</p>

      <div className="range-row" style={{ flexWrap: "wrap" }}>
        <div className="quick-ranges" style={{ gap: 10, flexWrap: "wrap" }}>
          <select value={agentId} onChange={(e) => updateFilter(setAgentId)(e.target.value)} className="report-select">
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name}{a.team ? ` · ${a.team}` : ""}</option>
            ))}
          </select>

          <select value={dispositionStatus} onChange={(e) => updateFilter(setDispositionStatus)(e.target.value)} className="report-select">
            {DISPOSITION_STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>

          <select value={actualDisposition} onChange={(e) => updateFilter(setActualDisposition)(e.target.value)} className="report-select">
            <option value="">Any actual disposition</option>
            {dispositionOptions.actual.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>

          <select value={predictedDisposition} onChange={(e) => updateFilter(setPredictedDisposition)(e.target.value)} className="report-select">
            <option value="">Any predicted disposition</option>
            {dispositionOptions.predicted.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>

          <select value={sort} onChange={(e) => updateFilter(setSort)(e.target.value)} className="report-select">
            {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          {data && <span className="hint">{data.total} audited call{data.total === 1 ? "" : "s"} in range</span>}
          <a href={downloadUrl} className="btn-primary" style={{ width: "auto", padding: "9px 18px", fontSize: 13, textDecoration: "none", display: "inline-block" }}>
            ⬇ Download CSV
          </a>
        </div>
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
                  <th>Actual Disposition</th>
                  <th>Predicted Disposition</th>
                  <th>Disposition Status</th>
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
                    <td className="report-disposition-cell">{row.actual_disposition || "—"}</td>
                    <td className="report-disposition-cell">{row.predicted_disposition || "—"}</td>
                    <td><DispositionStatusBadge match={row.disposition_match} /></td>
                    {data.columns.map((c) => (
                      <td key={c.key} className="mono report-score-cell">
                        {row[c.key] ?? "—"}
                      </td>
                    ))}
                  </tr>
                ))}
                {data?.rows.length === 0 && (
                  <tr><td colSpan={data.columns.length + 6} className="state-msg">No audited calls match these filters.</td></tr>
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