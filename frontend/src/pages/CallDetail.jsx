import { useEffect, useState } from "react";
import { api } from "../api";

function barColor(score) {
  if (score >= 80) return "var(--teal)";
  if (score >= 60) return "var(--amber)";
  return "var(--coral)";
}

export default function CallDetail({ callId, onBack }) {
  const [data, setData] = useState(null);
  const [transcript, setTranscript] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.callDetail(callId),
      api.callTranscript(callId).catch(() => null),
    ])
      .then(([callData, tData]) => {
        setData(callData);
        setTranscript(tData);
      })
      .catch((err) => setError(err.message || "Could not load call"))
      .finally(() => setLoading(false));
  }, [callId]);

  if (loading) return <div className="state-msg">Loading call scorecard…</div>;
  if (error) return <div className="login-error">{error}</div>;
  if (!data) return null;

  const { call, parameters, fatal_checks } = data;
  const triggeredFatal = fatal_checks.filter((f) => f.status);
  const sorted = [...parameters].sort((a, b) => (b.score || 0) - (a.score || 0));
  const strengths = sorted.slice(0, 3);
  const improvements = sorted.slice(-3).reverse();

  let utterances = [];
  try {
    utterances = transcript ? JSON.parse(transcript.utterances_json) : [];
  } catch {
    utterances = [];
  }

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">Back</button>
        <span>/</span>
        <span>Call #{call.id}</span>
      </div>

      <h1 className="page-title">{call.agent_name}</h1>
      <p className="page-sub">
        {call.call_service_name || "Call"} · {call.call_start_time || call.call_date}
        {call.recording_base ? ` · ${call.recording_base}` : ""}
      </p>

      {call.fatal_error === 1 && (
        <div className="login-error" style={{ marginBottom: 24 }}>
          Fatal compliance flag raised on this call — review immediately.
        </div>
      )}

      <div className="stat-grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
        <div className="stat-card accent-ink">
          <div className="label">Overall score</div>
          <div className="value mono">{call.overall_score ?? "—"}</div>
        </div>
        <div className="stat-card accent-teal">
          <div className="label">Verdict</div>
          <div className="value mono" style={{ fontSize: 20 }}>{call.verdict || "—"}</div>
        </div>
        <div className="stat-card accent-amber">
          <div className="label">Quality</div>
          <div className="value mono" style={{ fontSize: 20 }}>{call.overall_quality || "—"}</div>
        </div>
        <div className={`stat-card ${triggeredFatal.length ? "accent-coral" : "accent-teal"}`}>
          <div className="label">Fatal flags</div>
          <div className="value mono">{triggeredFatal.length}</div>
        </div>
      </div>

      {call.summary && (
        <div className="summary-card" style={{ marginBottom: 32 }}>
          <h3>Call summary</h3>
          <p style={{ fontSize: 13, lineHeight: 1.7, margin: 0 }}>{call.summary}</p>
        </div>
      )}

      <div className="section-head"><h2>Parameter-wise breakdown</h2></div>
      <div className="param-list">
        {parameters.map((p, i) => (
          <div className="param-row" key={p.parameter} title={p.reason}>
            <span className="idx">{String(i + 1).padStart(2, "0")}</span>
            <span className="name">{p.parameter}</span>
            <span className="bar-track">
              <span className="bar-fill" style={{ width: `${p.score}%`, background: barColor(p.score) }} />
            </span>
            <span className="score mono">{p.score}</span>
          </div>
        ))}
      </div>

      <div className="summary-grid">
        <div className="summary-card strength">
          <h3>Strengths</h3>
          <ul>{strengths.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
        </div>
        <div className="summary-card improve">
          <h3>Improvement areas</h3>
          <ul>{improvements.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
        </div>
      </div>

      <div className="section-head" style={{ marginTop: 40 }}><h2>Transcript</h2></div>
      {utterances.length > 0 ? (
        <div className="param-list" style={{ padding: "4px 0" }}>
          {utterances.map((u, i) => (
            <div key={i} className="param-row" style={{ gridTemplateColumns: "80px 60px 1fr" }}>
              <span className="idx mono">{u.start?.toFixed ? u.start.toFixed(1) + "s" : u.start}</span>
              <span className="name" style={{ fontWeight: 600, color: u.speaker === "Agent" ? "var(--teal)" : "var(--ink)" }}>
                {u.speaker}
              </span>
              <span className="name">{u.text}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="state-msg">No transcript stored for this call.</div>
      )}
    </div>
  );
}
