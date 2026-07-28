import { useEffect, useRef, useState } from "react";
import { api } from "../api";

function barColor(score) {
  if (score >= 80) return "var(--teal)";
  if (score >= 60) return "var(--amber)";
  return "var(--coral)";
}

function fmtTime(t) {
  if (t === null || t === undefined) return "";
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function CallDetail({ callId, onBack }) {
  const [data, setData] = useState(null);
  const [transcript, setTranscript] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  // Which recording leg the "Listen" buttons control. Both legs share the
  // same wall-clock timeline (call start = 0s), so a parameter's start_time
  // seeks correctly on either one - the toggle just decides which audio you
  // actually hear (agent voice vs. customer voice).
  const [activeLeg, setActiveLeg] = useState("agent"); // 'agent' -> tx_path (OUT), 'customer' -> rx_path (IN)
  const agentAudioRef = useRef(null);
  const customerAudioRef = useRef(null);
  const [nowPlaying, setNowPlaying] = useState(null); // parameter name currently being auditioned

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
  const hasAudio = Boolean(call.tx_path || call.rx_path);

  let utterances = [];
  try {
    utterances = transcript ? JSON.parse(transcript.utterances_json) : [];
  } catch {
    utterances = [];
  }

  // Seek the chosen leg to this parameter's evidence timestamp and play it,
  // pausing the other leg so audio doesn't overlap.
  function playEvidence(paramName, startTime) {
    if (startTime === null || startTime === undefined) return;
    const target = activeLeg === "agent" ? agentAudioRef.current : customerAudioRef.current;
    const other = activeLeg === "agent" ? customerAudioRef.current : agentAudioRef.current;
    if (!target) return;
    if (other) other.pause();
    target.currentTime = startTime;
    target.play();
    setNowPlaying(paramName);
  }

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">Back</button>
        <span>/</span>
        <span>Call #{call.id}</span>
        {console.log(call)}
        {/* <h2>clicked to get the detial of the call </h2> */}
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

      {/* ---- Feature 2.3: call recording player ---- */}
      {hasAudio && (
        <div className="summary-card" style={{ marginBottom: 32 }}>
          <h3>Call recording</h3>
          <div className="leg-toggle">
            <button
              type="button"
              className={activeLeg === "agent" ? "active" : ""}
              onClick={() => setActiveLeg("agent")}
              disabled={!call.tx_path}
            >
              Agent leg (OUT)
            </button>
            <button
              type="button"
              className={activeLeg === "customer" ? "active" : ""}
              onClick={() => setActiveLeg("customer")}
              disabled={!call.rx_path}
            >
              Customer leg (IN)
            </button>
          </div>
          {call.tx_path && (
            <audio
              ref={agentAudioRef}
              controls
              src={call.tx_path}
              style={{ width: "100%", marginTop: 10, display: activeLeg === "agent" ? "block" : "none" }}
            />
          )}
          {call.rx_path && (
            <audio
              ref={customerAudioRef}
              controls
              src={call.rx_path}
              style={{ width: "100%", marginTop: 10, display: activeLeg === "customer" ? "block" : "none" }}
            />
          )}
          {/* <p className="hint" style={{ marginTop: 10 }}>
            Use "Listen" next to any parameter below to jump straight to the moment that score was decided.
          </p> */}
        </div>
      )}

      <div className="section-head"><h2>Parameter-wise breakdown</h2></div>
      <div className="param-list">
        <h1>parameters value </h1>
        {console.log("parameters", parameters)}
        <p>***********************************</p>
        {parameters.map((p, i) => (

          <div className="param-row-detailed" key={p.parameter}>
            <div className="param-row-main">
              <span className="idx">{String(i + 1).padStart(2, "0")}</span>
              <span className="name">{p.parameter}</span>
              <span className="bar-track">
                <span className="bar-fill" style={{ width: `${p.raw_score}%`, background: barColor(p.raw_score) }} />
              </span>
              <span className="score mono">{p.raw_score}</span>
              {hasAudio && p.start_time !== null && p.start_time !== undefined && (
                <button
                  type="button"
                  className="listen-btn"
                  onClick={() => playEvidence(p.parameter, p.start_time)}
                  title={`Play from ${fmtTime(p.start_time)}`}
                >
                  {nowPlaying === p.parameter ? "▶ Playing" : "▶ Listen"}
                </button>
              )}
            </div>
            {(p.reason || p.evidence) && (
              <div className="param-row-evidence">
                {p.reason && <span>{p.reason}</span>}
                {p.evidence && (
                  <span className="evidence-quote">
                    "{p.evidence}"
                    {p.start_time !== null && p.start_time !== undefined && (
                      <span className="mono" style={{ color: "var(--gray)" }}> · {fmtTime(p.start_time)}</span>
                    )}
                  </span>
                )}
              </div>
            )}
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
