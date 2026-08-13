import { useEffect, useRef, useState } from "react";
import { api } from "../api";

function barColor(score) {
  if (score >= 80) return "var(--teal)";
  if (score >= 60) return "var(--amber)";
  return "var(--coral)";
}

function fmtTime(t) {
  if (t === null || t === undefined) return "--:--";
  // const s=int(t)%100
  // const m=(int(t)/100)%100
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function CallDetail({ callId, onBack }) {
  const [data, setData] = useState(null);
  const [transcript, setTranscript] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  // Which recording the "Listen" buttons control. All three share the same
  // wall-clock timeline (call start = 0s), so a parameter's start_time seeks
  // correctly on any of them - this just decides which one you actually hear.
  // 'mono' = single combined recording (recommended - always available if
  // the recordings server has it). 'agent'/'customer' = separate OUT/IN legs,
  // kept for calls where only those were captured.
  const [activeLeg, setActiveLeg] = useState("mono");
  const monoAudioRef = useRef(null);
  const agentAudioRef = useRef(null);
  const customerAudioRef = useRef(null);
  const refForLeg = { mono: monoAudioRef, agent: agentAudioRef, customer: customerAudioRef };

  // Full evidence window of whatever's playing, so we can auto-stop at
  // end_time and highlight the matching transcript line while it plays.
  const [nowPlaying, setNowPlaying] = useState(null); // { parameter, start_time, end_time }

  useEffect(() => {
    setLoading(true);
    setError("");
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

  // Auto-stop playback right at the parameter's evidence end_time, on
  // whichever recording is currently active.
  useEffect(() => {
    const audio = refForLeg[activeLeg]?.current;
    if (!audio || !nowPlaying || nowPlaying.end_time === null || nowPlaying.end_time === undefined) return;
    function onTimeUpdate() {
      if (audio.currentTime >= nowPlaying.end_time) {
        audio.pause();
      }
    }
    audio.addEventListener("timeupdate", onTimeUpdate);
    return () => audio.removeEventListener("timeupdate", onTimeUpdate);
  }, [nowPlaying, activeLeg]);

  if (loading) return <div className="state-msg">Loading call scorecard…</div>;
  if (error && !data) return <div className="login-error">{error}</div>;
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

  // Only treat a source as playable if it's an actual non-empty URL. Any of
  // mono_url/rx_path/tx_path can legitimately be null/"" (recording not
  // matched, or that leg was never captured) - handing an empty src to
  // <audio> and then calling play() is what throws "NotSupportedError".
  const monoSrc = call.mono_url || null;
  const agentSrc = call.tx_path || null;
  const customerSrc = call.rx_path || null;
  const srcForLeg = { mono: monoSrc, agent: agentSrc, customer: customerSrc };
  const hasAnyAudio = Boolean(monoSrc || agentSrc || customerSrc);
  const activeSrc = srcForLeg[activeLeg];

  // Seek the active recording to this parameter's evidence timestamp and
  // play it, pausing the other two so audio never overlaps.
  // function playEvidence(paramName, startTime, endTime) {
  //   {console.log(`playevidenc is ${paramName} ${startTime} ${endTime}`)}
  //   if (startTime === null || startTime === undefined) return;

  //   const target = refForLeg[activeLeg]?.current;
  //   if (!target || !activeSrc) {
  //     const legLabel = activeLeg === "mono" ? "mono" : activeLeg === "agent" ? "agent (OUT)" : "customer (IN)";
  //     setError(`No ${legLabel} recording is available for this call.`);
  //     return;
  //   }

  //   Object.entries(refForLeg).forEach(([leg, ref]) => {
  //     if (leg !== activeLeg && ref.current) ref.current.pause();
  //   });

  //   setError("");
  //   target.currentTime = startTime;
  //   target.play().catch((err) => setError(`Couldn't play recording: ${err.message}`));
  //   setNowPlaying({ parameter: paramName, start_time: startTime, end_time: endTime });
  // }
  function playEvidence(paramName, startTime, endTime) {
  if (startTime === null || startTime === undefined) return;
  // gettin the actual start time to run the recording M*60+seconds to get the total in seconds only
  startTime=Math.trunc(Math.trunc(startTime)/100)*60 + Math.trunc(startTime)%100
  // to end will take the higher value 
  endTime=Math.trunc(Math.ceil(endTime)/100)*60+ Math.ceil(endTime)%100
  const target = refForLeg[activeLeg]?.current;
  if (!target || !activeSrc) {
    setError(`No ${activeLeg} recording is available for this call.`);
    return;
  }
  Object.entries(refForLeg).forEach(([leg, ref]) => {
    if (leg !== activeLeg && ref.current) ref.current.pause();
  });
  setError("");

  function seekAndPlay() {
    if (startTime > target.duration) {
      setError(`This recording is only ${target.duration.toFixed(1)}s long — can't seek to ${startTime}s. Check the recording matches this call.`);
      return;
    }
    target.currentTime = startTime;
    target.play().catch((err) => setError(`Couldn't play recording: ${err.message}`));
  }

  if (target.readyState >= 1) {
    seekAndPlay();
  } else {
    target.addEventListener("loadedmetadata", seekAndPlay, { once: true });
  }

  setNowPlaying({ parameter: paramName, start_time: startTime, end_time: endTime });
}

  const LEG_OPTIONS = [
    { key: "mono", label: "Full call (mono)", available: Boolean(monoSrc) },
    // { key: "agent", label: "Agent leg (OUT)", available: Boolean(agentSrc) },
    // { key: "customer", label: "Customer leg (IN)", available: Boolean(customerSrc) },
  ];

  return (
    <div>
      <div className="breadcrumb">
        <button onClick={onBack} type="button">Back</button>
        <span>/</span>
        <span>Call #{call.id}</span>
      </div>
      {/* {console.log(call)} */}

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
          <div className="label">Total score</div>
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

      {/* Correct/Wrong feature: shows the actual (manifest) vs predicted (SLM)
          disposition side by side, so clicking into a call from either the
          "Correct" or "Wrong" dashboard button immediately explains itself -
          no need to go dig through the transcript to see why it landed there.
          The SLM's disposition is a full evidence entry just like a
          parameter (reason + quoted evidence + timestamps), so "Listen"
          here reuses the exact same playEvidence()/seek/highlight flow as
          every parameter row below - no separate audio logic needed.
          Only rendered when there's something to compare; a call missing
          either side (disposition_match is null) just doesn't show this card. */}
      {call.disposition_match !== null && call.disposition_match !== undefined && (
        <div className="summary-card" style={{ marginBottom: 32 }}>
          <h3>Disposition check</h3>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", alignItems: "center", marginBottom: call.disposition_reason ? 12 : 0 }}>
            <div>
              <div style={{ fontSize: 11, color: "var(--gray)", textTransform: "uppercase", marginBottom: 4 }}>Actual (manifest)</div>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{call.call_end_type_name || "—"}</div>
            </div>
            <div>
              <div style={{ fontSize: 11, color: "var(--gray)", textTransform: "uppercase", marginBottom: 4 }}>Predicted (AI)</div>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{call.predicted_disposition || "—"}</div>
            </div>
            <span className={`score-chip ${call.disposition_match ? "good" : "poor"}`}>
              {call.disposition_match ? "Correct" : "Wrong"}
            </span>
            {hasAnyAudio && call.disposition_start_time !== null && call.disposition_start_time !== undefined && (
              <button
                type="button"
                className="listen-btn"
                onClick={() => playEvidence("__disposition__", call.disposition_start_time, call.disposition_end_time)}
                title={`Play ${fmtTime(call.disposition_start_time)} - ${fmtTime(call.disposition_end_time)}`}
              >
                {nowPlaying?.parameter === "__disposition__" ? "▶ Playing" : "▶ Listen"}
              </button>
            )}
          </div>

          {call.disposition_reason && (
            <div style={{ fontSize: 12, color: "var(--gray)" }}>
              {call.disposition_reason}
              {call.disposition_evidence && (
                <span style={{ color: "var(--ink-soft)" }}> — "{call.disposition_evidence}"</span>
              )}
            </div>
          )}

          {(call.disposition_callback_date || call.disposition_callback_time) && (
            <div style={{ fontSize: 12, color: "var(--gray)", marginTop: 8 }}>
              Callback scheduled: <span style={{ color: "var(--ink)", fontWeight: 600 }}>
                {[call.disposition_callback_date, call.disposition_callback_time].filter(Boolean).join(" ")}
              </span>
            </div>
          )}
        </div>
      )}

      <div className="summary-card" style={{ marginBottom: 32 }}>
        <h3>Call recording</h3>

        {/* <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
          {LEG_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              type="button"
              className="btn-primary"
              style={{
                width: "auto",
                padding: "8px 16px",
                fontSize: 13,
                background: activeLeg === opt.key ? "var(--ink)" : "white",
                color: activeLeg === opt.key ? "white" : "var(--ink)",
                border: "1px solid var(--line)",
                opacity: opt.available ? 1 : 0.5,
              }}
              onClick={() => setActiveLeg(opt.key)}
            >
              {opt.label}{!opt.available && " · unavailable"}
            </button>
          ))}
        </div> */}

        {error && <div className="login-error">{error}</div>}

        {!hasAnyAudio && (
          <div className="state-msg" style={{ padding: "20px 0" }}>
            No recording URL match Or recording deleted from the server
            {/* saved for this call — nothing to play. This means
            the manifest pipeline couldn't build a playable path (recording
            base not matched, or the recordings base URL wasn't set) when
            this call was audited. */}
          </div>
        )}

        {/* All three are mounted at once (hidden when inactive) so their
            currentTime survives toggling; only render a real src when we
            actually have one, otherwise the browser throws NotSupportedError
            the instant play() is called on an empty tag. */}
        <audio
          ref={monoAudioRef}
          controls
          src={monoSrc || undefined}
          style={{ width: "100%", display: activeLeg === "mono" ? "block" : "none" }}
        />
        <audio
          ref={agentAudioRef}
          controls
          src={agentSrc || undefined}
          style={{ width: "100%", display: activeLeg === "agent" ? "block" : "none" }}
        />
        <audio
          ref={customerAudioRef}
          controls
          src={customerSrc || undefined}
          style={{ width: "100%", display: activeLeg === "customer" ? "block" : "none" }}
        />
      </div>

      <div className="section-head"><h2>Parameter-wise breakdown</h2></div>
      <div className="score-table-wrap">
        <table className="score-table">
          <colgroup>
            <col className="col-idx" />
            <col className="col-param" />
            <col className="col-score" />
            <col className="col-action" />
          </colgroup>
          <thead>
            {/* <tr>
              <th scope="col">#</th>
              <th scope="col">Parameter</th>
              <th scope="col">Score</th>
              <th scope="col">Evidence</th>
            </tr> */}
          </thead>
          <tbody>
            {parameters.map((p, i) => (
              <tr
                key={p.parameter}
                className={nowPlaying?.parameter === p.parameter ? "is-playing" : ""}
              >
                <td className="mono idx-cell">{String(i + 1).padStart(2, "0")}</td>
                <td>
                  <div className="param-name">{p.parameter}</div>
                  {(p.reason || p.evidence) && (
                    <div className="param-evidence">
                      {p.reason}
                      {p.evidence && (
                        <span className="evidence-quote">
                          {" "}— "{p.evidence}"
                          {p.start_time !== null && p.start_time !== undefined && (
                            <span className="mono" style={{ color: "var(--gray)" }}> · {fmtTime(p.start_time)}</span>
                          )}
                        </span>
                      )}
                    </div>
                  )}
                </td>
                <td>
                  <div className="score-cell">
                    <span className="bar-track">
                      <span className="bar-fill" style={{ width: `${p.score}%`, background: barColor(p.score) }} />
                    </span>
                    <span className="score mono">{p.score}</span>
                  </div>
                </td>
                <td>
                  {hasAnyAudio && p.start_time !== null && p.start_time !== undefined ? (
                    <button
                      type="button"
                      className="listen-btn"
                      onClick={() => playEvidence(p.parameter, p.start_time, p.end_time)}
                      title={`Play ${fmtTime(p.start_time)} - ${fmtTime(p.end_time)}`}
                    >
                      {nowPlaying?.parameter === p.parameter ? "▶ Playing" : "▶ Listen"}
                    </button>
                  ) : (
                    <span className="mono no-evidence">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
          {utterances.map((u, i) => {
            const isHighlighted =
              nowPlaying &&
              u.end >= nowPlaying.start_time &&
              u.start <= (nowPlaying.end_time ?? nowPlaying.start_time + 0.01);
            return (
              <div
                key={i}
                className={`param-row ${isHighlighted ? "is-playing" : ""}`}
                style={{ gridTemplateColumns: "80px 60px 1fr" }}
              >
                <span className="idx mono">{u.start?.toFixed ? u.start.toFixed(1) + "s" : u.start}</span>
                <span className="name" style={{ fontWeight: 600, color: u.speaker === "Agent" ? "var(--teal)" : "var(--ink)" }}>
                  {u.speaker}
                </span>
                <span className="name">{u.text}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="state-msg">No transcript stored for this call.</div>
      )}
    </div>
  );
}

// **************************************4 th way**********************************************
// import { useEffect, useRef, useState } from "react";
// import { api } from "../api";

// function barColor(score) {
//   if (score >= 80) return "var(--teal)";
//   if (score >= 60) return "var(--amber)";
//   return "var(--coral)";
// }

// function fmtTime(t) {
//   if (t === null || t === undefined) return "--:--";
//   // const s=int(t)%100
//   // const m=(int(t)/100)%100
//   const m = Math.floor(t / 60);
//   const s = Math.floor(t % 60);
//   return `${m}:${String(s).padStart(2, "0")}`;
// }

// export default function CallDetail({ callId, onBack }) {
//   const [data, setData] = useState(null);
//   const [transcript, setTranscript] = useState(null);
//   const [error, setError] = useState("");
//   const [loading, setLoading] = useState(true);

//   // Which recording the "Listen" buttons control. All three share the same
//   // wall-clock timeline (call start = 0s), so a parameter's start_time seeks
//   // correctly on any of them - this just decides which one you actually hear.
//   // 'mono' = single combined recording (recommended - always available if
//   // the recordings server has it). 'agent'/'customer' = separate OUT/IN legs,
//   // kept for calls where only those were captured.
//   const [activeLeg, setActiveLeg] = useState("mono");
//   const monoAudioRef = useRef(null);
//   const agentAudioRef = useRef(null);
//   const customerAudioRef = useRef(null);
//   const refForLeg = { mono: monoAudioRef, agent: agentAudioRef, customer: customerAudioRef };

//   // Full evidence window of whatever's playing, so we can auto-stop at
//   // end_time and highlight the matching transcript line while it plays.
//   const [nowPlaying, setNowPlaying] = useState(null); // { parameter, start_time, end_time }

//   useEffect(() => {
//     setLoading(true);
//     setError("");
//     Promise.all([
//       api.callDetail(callId),
//       api.callTranscript(callId).catch(() => null),
//     ])
//       .then(([callData, tData]) => {
//         setData(callData);
//         setTranscript(tData);
//       })
//       .catch((err) => setError(err.message || "Could not load call"))
//       .finally(() => setLoading(false));
//   }, [callId]);

//   // Auto-stop playback right at the parameter's evidence end_time, on
//   // whichever recording is currently active.
//   useEffect(() => {
//     const audio = refForLeg[activeLeg]?.current;
//     if (!audio || !nowPlaying || nowPlaying.end_time === null || nowPlaying.end_time === undefined) return;
//     function onTimeUpdate() {
//       if (audio.currentTime >= nowPlaying.end_time) {
//         audio.pause();
//       }
//     }
//     audio.addEventListener("timeupdate", onTimeUpdate);
//     return () => audio.removeEventListener("timeupdate", onTimeUpdate);
//   }, [nowPlaying, activeLeg]);

//   if (loading) return <div className="state-msg">Loading call scorecard…</div>;
//   if (error && !data) return <div className="login-error">{error}</div>;
//   if (!data) return null;

//   const { call, parameters, fatal_checks } = data;
//   const triggeredFatal = fatal_checks.filter((f) => f.status);
//   const sorted = [...parameters].sort((a, b) => (b.score || 0) - (a.score || 0));
//   const strengths = sorted.slice(0, 3);
//   const improvements = sorted.slice(-3).reverse();

//   let utterances = [];
//   try {
//     utterances = transcript ? JSON.parse(transcript.utterances_json) : [];
//   } catch {
//     utterances = [];
//   }

//   // Only treat a source as playable if it's an actual non-empty URL. Any of
//   // mono_url/rx_path/tx_path can legitimately be null/"" (recording not
//   // matched, or that leg was never captured) - handing an empty src to
//   // <audio> and then calling play() is what throws "NotSupportedError".
//   const monoSrc = call.mono_url || null;
//   const agentSrc = call.tx_path || null;
//   const customerSrc = call.rx_path || null;
//   const srcForLeg = { mono: monoSrc, agent: agentSrc, customer: customerSrc };
//   const hasAnyAudio = Boolean(monoSrc || agentSrc || customerSrc);
//   const activeSrc = srcForLeg[activeLeg];

//   // Seek the active recording to this parameter's evidence timestamp and
//   // play it, pausing the other two so audio never overlaps.
//   // function playEvidence(paramName, startTime, endTime) {
//   //   {console.log(`playevidenc is ${paramName} ${startTime} ${endTime}`)}
//   //   if (startTime === null || startTime === undefined) return;

//   //   const target = refForLeg[activeLeg]?.current;
//   //   if (!target || !activeSrc) {
//   //     const legLabel = activeLeg === "mono" ? "mono" : activeLeg === "agent" ? "agent (OUT)" : "customer (IN)";
//   //     setError(`No ${legLabel} recording is available for this call.`);
//   //     return;
//   //   }

//   //   Object.entries(refForLeg).forEach(([leg, ref]) => {
//   //     if (leg !== activeLeg && ref.current) ref.current.pause();
//   //   });

//   //   setError("");
//   //   target.currentTime = startTime;
//   //   target.play().catch((err) => setError(`Couldn't play recording: ${err.message}`));
//   //   setNowPlaying({ parameter: paramName, start_time: startTime, end_time: endTime });
//   // }
//   function playEvidence(paramName, startTime, endTime) {
//   if (startTime === null || startTime === undefined) return;
//   // gettin the actual start time to run the recording M*60+seconds to get the total in seconds only
//   startTime=Math.trunc(Math.trunc(startTime)/100)*60 + Math.trunc(startTime)%100
//   // to end will take the higher value 
//   endTime=Math.trunc(Math.ceil(endTime)/100)*60+ Math.ceil(endTime)%100
//   const target = refForLeg[activeLeg]?.current;
//   if (!target || !activeSrc) {
//     setError(`No ${activeLeg} recording is available for this call.`);
//     return;
//   }
//   Object.entries(refForLeg).forEach(([leg, ref]) => {
//     if (leg !== activeLeg && ref.current) ref.current.pause();
//   });
//   setError("");

//   function seekAndPlay() {
//     if (startTime > target.duration) {
//       setError(`This recording is only ${target.duration.toFixed(1)}s long — can't seek to ${startTime}s. Check the recording matches this call.`);
//       return;
//     }
//     target.currentTime = startTime;
//     target.play().catch((err) => setError(`Couldn't play recording: ${err.message}`));
//   }

//   if (target.readyState >= 1) {
//     seekAndPlay();
//   } else {
//     target.addEventListener("loadedmetadata", seekAndPlay, { once: true });
//   }

//   setNowPlaying({ parameter: paramName, start_time: startTime, end_time: endTime });
// }

//   const LEG_OPTIONS = [
//     { key: "mono", label: "Full call (mono)", available: Boolean(monoSrc) },
//     // { key: "agent", label: "Agent leg (OUT)", available: Boolean(agentSrc) },
//     // { key: "customer", label: "Customer leg (IN)", available: Boolean(customerSrc) },
//   ];

//   return (
//     <div>
//       <div className="breadcrumb">
//         <button onClick={onBack} type="button">Back</button>
//         <span>/</span>
//         <span>Call #{call.id}</span>
//       </div>
//       {/* {console.log(call)} */}

//       <h1 className="page-title">{call.agent_name}</h1>
//       <p className="page-sub">
//         {call.call_service_name || "Call"} · {call.call_start_time || call.call_date}
//         {call.recording_base ? ` · ${call.recording_base}` : ""}
//       </p>

//       {call.fatal_error === 1 && (
//         <div className="login-error" style={{ marginBottom: 24 }}>
//           Fatal compliance flag raised on this call — review immediately.
//         </div>
//       )}

//       <div className="stat-grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
//         <div className="stat-card accent-ink">
//           <div className="label">Overall score</div>
//           <div className="value mono">{call.overall_score ?? "—"}</div>
//         </div>
//         <div className="stat-card accent-teal">
//           <div className="label">Verdict</div>
//           <div className="value mono" style={{ fontSize: 20 }}>{call.verdict || "—"}</div>
//         </div>
//         <div className="stat-card accent-amber">
//           <div className="label">Quality</div>
//           <div className="value mono" style={{ fontSize: 20 }}>{call.overall_quality || "—"}</div>
//         </div>
//         <div className={`stat-card ${triggeredFatal.length ? "accent-coral" : "accent-teal"}`}>
//           <div className="label">Fatal flags</div>
//           <div className="value mono">{triggeredFatal.length}</div>
//         </div>
//       </div>

//       {call.summary && (
//         <div className="summary-card" style={{ marginBottom: 32 }}>
//           <h3>Call summary</h3>
//           <p style={{ fontSize: 13, lineHeight: 1.7, margin: 0 }}>{call.summary}</p>
//         </div>
//       )}

//       <div className="summary-card" style={{ marginBottom: 32 }}>
//         <h3>Call recording</h3>

//         {/* <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
//           {LEG_OPTIONS.map((opt) => (
//             <button
//               key={opt.key}
//               type="button"
//               className="btn-primary"
//               style={{
//                 width: "auto",
//                 padding: "8px 16px",
//                 fontSize: 13,
//                 background: activeLeg === opt.key ? "var(--ink)" : "white",
//                 color: activeLeg === opt.key ? "white" : "var(--ink)",
//                 border: "1px solid var(--line)",
//                 opacity: opt.available ? 1 : 0.5,
//               }}
//               onClick={() => setActiveLeg(opt.key)}
//             >
//               {opt.label}{!opt.available && " · unavailable"}
//             </button>
//           ))}
//         </div> */}

//         {error && <div className="login-error">{error}</div>}

//         {!hasAnyAudio && (
//           <div className="state-msg" style={{ padding: "20px 0" }}>
//             No recording URL match Or recording deleted from the server
//             {/* saved for this call — nothing to play. This means
//             the manifest pipeline couldn't build a playable path (recording
//             base not matched, or the recordings base URL wasn't set) when
//             this call was audited. */}
//           </div>
//         )}

//         {/* All three are mounted at once (hidden when inactive) so their
//             currentTime survives toggling; only render a real src when we
//             actually have one, otherwise the browser throws NotSupportedError
//             the instant play() is called on an empty tag. */}
//         <audio
//           ref={monoAudioRef}
//           controls
//           src={monoSrc || undefined}
//           style={{ width: "100%", display: activeLeg === "mono" ? "block" : "none" }}
//         />
//         <audio
//           ref={agentAudioRef}
//           controls
//           src={agentSrc || undefined}
//           style={{ width: "100%", display: activeLeg === "agent" ? "block" : "none" }}
//         />
//         <audio
//           ref={customerAudioRef}
//           controls
//           src={customerSrc || undefined}
//           style={{ width: "100%", display: activeLeg === "customer" ? "block" : "none" }}
//         />
//       </div>

//       <div className="section-head"><h2>Parameter-wise breakdown</h2></div>
//       <div className="score-table-wrap">
//         <table className="score-table">
//           <colgroup>
//             <col className="col-idx" />
//             <col className="col-param" />
//             <col className="col-score" />
//             <col className="col-action" />
//           </colgroup>
//           <thead>
//             {/* <tr>
//               <th scope="col">#</th>
//               <th scope="col">Parameter</th>
//               <th scope="col">Score</th>
//               <th scope="col">Evidence</th>
//             </tr> */}
//           </thead>
//           <tbody>
//             {parameters.map((p, i) => (
//               <tr
//                 key={p.parameter}
//                 className={nowPlaying?.parameter === p.parameter ? "is-playing" : ""}
//               >
//                 <td className="mono idx-cell">{String(i + 1).padStart(2, "0")}</td>
//                 <td>
//                   <div className="param-name">{p.parameter}</div>
//                   {(p.reason || p.evidence) && (
//                     <div className="param-evidence">
//                       {p.reason}
//                       {p.evidence && (
//                         <span className="evidence-quote">
//                           {" "}— "{p.evidence}"
//                           {p.start_time !== null && p.start_time !== undefined && (
//                             <span className="mono" style={{ color: "var(--gray)" }}> · {fmtTime(p.start_time)}</span>
//                           )}
//                         </span>
//                       )}
//                     </div>
//                   )}
//                 </td>
//                 <td>
//                   <div className="score-cell">
//                     <span className="bar-track">
//                       <span className="bar-fill" style={{ width: `${p.score}%`, background: barColor(p.score) }} />
//                     </span>
//                     <span className="score mono">{p.score}</span>
//                   </div>
//                 </td>
//                 <td>
//                   {hasAnyAudio && p.start_time !== null && p.start_time !== undefined ? (
//                     <button
//                       type="button"
//                       className="listen-btn"
//                       onClick={() => playEvidence(p.parameter, p.start_time, p.end_time)}
//                       title={`Play ${fmtTime(p.start_time)} - ${fmtTime(p.end_time)}`}
//                     >
//                       {nowPlaying?.parameter === p.parameter ? "▶ Playing" : "▶ Listen"}
//                     </button>
//                   ) : (
//                     <span className="mono no-evidence">—</span>
//                   )}
//                 </td>
//               </tr>
//             ))}
//           </tbody>
//         </table>
//       </div>


//       <div className="summary-grid">
//         <div className="summary-card strength">
//           <h3>Strengths</h3>
//           <ul>{strengths.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
//         </div>
//         <div className="summary-card improve">
//           <h3>Improvement areas</h3>
//           <ul>{improvements.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
//         </div>
//       </div> 

//       <div className="section-head" style={{ marginTop: 40 }}><h2>Transcript</h2></div>
//       {utterances.length > 0 ? (
//         <div className="param-list" style={{ padding: "4px 0" }}>
//           {utterances.map((u, i) => {
//             const isHighlighted =
//               nowPlaying &&
//               u.end >= nowPlaying.start_time &&
//               u.start <= (nowPlaying.end_time ?? nowPlaying.start_time + 0.01);
//             return (
//               <div
//                 key={i}
//                 className={`param-row ${isHighlighted ? "is-playing" : ""}`}
//                 style={{ gridTemplateColumns: "80px 60px 1fr" }}
//               >
//                 <span className="idx mono">{u.start?.toFixed ? u.start.toFixed(1) + "s" : u.start}</span>
//                 <span className="name" style={{ fontWeight: 600, color: u.speaker === "Agent" ? "var(--teal)" : "var(--ink)" }}>
//                   {u.speaker}
//                 </span>
//                 <span className="name">{u.text}</span>
//               </div>
//             );
//           })}
//         </div>
//       ) : (
//         <div className="state-msg">No transcript stored for this call.</div>
//       )}
//     </div>
//   );
// }
// ***************** 3rd way *********************
// import { useEffect, useRef, useState } from "react";
// import { api } from "../api";

// function barColor(score) {
//   if (score >= 80) return "var(--teal)";
//   if (score >= 60) return "var(--amber)";
//   return "var(--coral)";
// }

// function fmtTime(t) {
//   if (t === null || t === undefined) return "--:--";
//   const m = Math.floor(t / 60);
//   const s = Math.floor(t % 60);
//   return `${m}:${String(s).padStart(2, "0")}`;
// }

// export default function CallDetail({ callId, onBack }) {
//   const [data, setData] = useState(null);
//   const [transcript, setTranscript] = useState(null);
//   const [error, setError] = useState("");
//   const [loading, setLoading] = useState(true);

//   // Which recording leg the "Listen" buttons control. Both legs share the
//   // same wall-clock timeline (call start = 0s), so a parameter's start_time
//   // seeks correctly on either one - the toggle just decides which audio you
//   // actually hear (agent voice vs. customer voice).
//   const [activeLeg, setActiveLeg] = useState("agent"); // 'agent' -> tx_path (OUT), 'customer' -> rx_path (IN)
//   const agentAudioRef = useRef(null);
//   const customerAudioRef = useRef(null);
//   // Full evidence window of whatever's playing, so we can auto-stop at
//   // end_time and highlight the matching transcript line while it plays.
//   const [nowPlaying, setNowPlaying] = useState(null); // { parameter, start_time, end_time }

//   useEffect(() => {
//     setLoading(true);
//     setError("");
//     Promise.all([
//       api.callDetail(callId),
//       api.callTranscript(callId).catch(() => null),
//     ])
//       .then(([callData, tData]) => {
//         setData(callData);
//         setTranscript(tData);
//       })
//       .catch((err) => setError(err.message || "Could not load call"))
//       .finally(() => setLoading(false));
//   }, [callId]);

//   // Auto-stop playback right at the parameter's evidence end_time, on
//   // whichever leg is currently active.
//   useEffect(() => {
//     const audio = activeLeg === "agent" ? agentAudioRef.current : customerAudioRef.current;
//     if (!audio || !nowPlaying || nowPlaying.end_time === null || nowPlaying.end_time === undefined) return;
//     function onTimeUpdate() {
//       if (audio.currentTime >= nowPlaying.end_time) {
//         audio.pause();
//       }
//     }
//     audio.addEventListener("timeupdate", onTimeUpdate);
//     return () => audio.removeEventListener("timeupdate", onTimeUpdate);
//   }, [nowPlaying, activeLeg]);

//   if (loading) return <div className="state-msg">Loading call scorecard…</div>;
//   if (error && !data) return <div className="login-error">{error}</div>;
//   if (!data) return null;

//   const { call, parameters, fatal_checks } = data;
//   const triggeredFatal = fatal_checks.filter((f) => f.status);
//   const sorted = [...parameters].sort((a, b) => (b.score || 0) - (a.score || 0));
//   const strengths = sorted.slice(0, 3);
//   const improvements = sorted.slice(-3).reverse();

//   let utterances = [];
//   try {
//     utterances = transcript ? JSON.parse(transcript.utterances_json) : [];
//   } catch {
//     utterances = [];
//   }

//   // Real fix for "NotSupportedError: no supported sources": only treat a leg
//   // as playable if it has an actual non-empty URL. call.rx_path/tx_path come
//   // back as null (never set), "" (pipeline couldn't find the recording), or
//   // undefined (key missing) just as often as a real URL - any of those must
//   // NOT be handed to <audio src=...>, or clicking play throws instantly.
//   const agentSrc = call.tx_path || null;
//   const customerSrc = call.rx_path || null;
//   const hasAnyAudio = Boolean(agentSrc || customerSrc);
//   const activeSrc = activeLeg === "agent" ? agentSrc : customerSrc;
  

//   // Seek the chosen leg to this parameter's evidence timestamp and play it,
//   // pausing the other leg so audio doesn't overlap.
//   function playEvidence(paramName, startTime, endTime) {
//     if (startTime === null || startTime === undefined) return;
//     console.log("playEvidence", paramName, startTime, endTime, activeLeg, agentAudioRef.current, customerAudioRef.current);
//     const target = activeLeg === "agent" ? agentAudioRef.current : customerAudioRef.current;
//     const other = activeLeg === "agent" ? customerAudioRef.current : agentAudioRef.current;

//     if (!target || !activeSrc) {
//       setError(
//         `No ${activeLeg === "agent" ? "agent (OUT)" : "customer (IN)"} recording is available for this call. ` +
//         `Check that rx_path/tx_path were saved for call #${call.id} and that the URL is reachable.`
//       );
//       return;
//     }

//     setError("");
//     if (other) other.pause();
//     target.currentTime = startTime;
//     target.play().catch((err) => {
//       setError(`Couldn't play recording: ${err.message}`);
//     });
//     setNowPlaying({ parameter: paramName, start_time: startTime, end_time: endTime });
//   }
//   // const audioUrl = "http://localhost:8000/audio/7002_06397853103_17-Jun-26-19-02-49.wav";

//   return (
      
//     <div>
//       <div className="breadcrumb">
//         <button onClick={onBack} type="button">Back</button>
//         <span>/</span>
//         <span>Call #{call.id}</span>
//       </div>

//     <div>
//       <audio controls src="http://192.168.11.253:9000/recordings/7002_06397853103_17-Jun-26-19-02-49.wav16">
//         Your browser does not support the audio element.
//       </audio>
//       <h5>/public/{call.recording_base}</h5>
//     </div>

// {/* <p>tetsing the the recoridng is coming or not </p>
//     <div>
//       <audio controls src={`http://192.168.11.253:9000/recordings/${call.recording_base}`}>
//         Your browser does not support the audio element.
//       </audio>
//       <h5>/public/{call.recording_base}</h5>
//     </div> */}

//       <h1 className="page-title">{call.agent_name}</h1> 
//       <p className="page-sub"> 
//         {call.call_service_name || "Call"} · {call.call_start_time || call.call_date}
//         {call.recording_base ? ` · ${call.recording_base}` : ""}
//       </p>

//       {call.fatal_error === 1 && (
//         <div className="login-error" style={{ marginBottom: 24 }}>
//           Fatal compliance flag raised on this call — review immediately.
//         </div>
//       )}

//       <div className="stat-grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
//         <div className="stat-card accent-ink">
//           <div className="label">Overall score</div>
//           <div className="value mono">{call.overall_score ?? "—"}</div>
//         </div>
//         <div className="stat-card accent-teal">
//           <div className="label">Verdict</div>
//           <div className="value mono" style={{ fontSize: 20 }}>{call.verdict || "—"}</div>
//         </div>
//         <div className="stat-card accent-amber">
//           <div className="label">Quality</div>
//           <div className="value mono" style={{ fontSize: 20 }}>{call.overall_quality || "—"}</div>
//         </div>
//         <div className={`stat-card ${triggeredFatal.length ? "accent-coral" : "accent-teal"}`}>
//           <div className="label">Fatal flags</div>
//           <div className="value mono">{triggeredFatal.length}</div>
//         </div>
//       </div>

//       {call.summary && (
//         <div className="summary-card" style={{ marginBottom: 32 }}>
//           <h3>Call summary</h3>
//           <p style={{ fontSize: 13, lineHeight: 1.7, margin: 0 }}>{call.summary}</p>
//         </div>
//       )}

//       <div className="summary-card" style={{ marginBottom: 32 }}>
//         <h3>Call recording</h3>

//         <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
//           <button
//             type="button"
//             className="btn-primary"
//             style={{
//               width: "auto",
//               padding: "8px 16px",
//               fontSize: 13,
//               background: activeLeg === "agent" ? "var(--ink)" : "white",
//               color: activeLeg === "agent" ? "white" : "var(--ink)",
//               border: "1px solid var(--line)",
//             }}
//             onClick={() => setActiveLeg("agent")}
//           >
//             Agent leg (OUT){!agentSrc && " · unavailable"}
//           </button>
//           <button
//             type="button"
//             className="btn-primary"
//             style={{
//               width: "auto",
//               padding: "8px 16px",
//               fontSize: 13,
//               background: activeLeg === "customer" ? "var(--ink)" : "white",
//               color: activeLeg === "customer" ? "white" : "var(--ink)",
//               border: "1px solid var(--line)",
//             }}
//             onClick={() => setActiveLeg("customer")}
//           >
//             Customer leg (IN){!customerSrc && " · unavailable"}
//           </button>
//         </div>

//         {error && <div className="login-error">{error}</div>}

//         {!hasAnyAudio && (
//           <div className="state-msg" style={{ padding: "20px 0" }}>
//             No recording URL saved for this call — nothing to play. This means
//             the manifest pipeline couldn't build rx_path/tx_path (recording
//             base not matched, or RECORDINGS_BASE_URL wasn't set) when this
//             call was audited.
//           </div>
//         )}
// {/* C:\Users\kishor\Desktop\offline\ybl27rec\7002_06397853103_17-Jun-26-19-02-49.wav16
//       <audio controls>
//         <source src={sampleAudio} type="audio/mpeg" />
//         Your browser does not support the audio element.
//       </audio> */}
//         {/* Both legs are mounted at once (hidden when inactive) so their
//             currentTime survives toggling between legs; only render a real
//             src when we actually have one, otherwise the browser throws
//             NotSupportedError the instant play() is called on an empty tag. */}
//         <audio
//           ref={agentAudioRef}
//           controls
//           src={agentSrc || undefined}
//           style={{ width: "100%", display: activeLeg === "agent" ? "block" : "none" }}
//         />
//         <audio
//           ref={customerAudioRef}
//           controls
//           src={customerSrc || undefined}
//           style={{ width: "100%", display: activeLeg === "customer" ? "block" : "none" }}
//         />
        
//       </div>

          

//       <div className="section-head"><h2>Parameter-wise breakdown</h2></div>
//       <div className="param-list">
//         {parameters.map((p, i) => (
//           <div
//             className={`param-row-detailed ${nowPlaying?.parameter === p.parameter ? "is-playing" : ""}`}
//             key={p.parameter}
//           >
//             <div className="param-row-main">
//               <span className="idx">{String(i + 1).padStart(2, "0")}</span>
//               <span className="name">{p.parameter}</span>
//               <span className="bar-track">
//                 <span className="bar-fill" style={{ width: `${p.score}%`, background: barColor(p.score) }} />
//               </span>
//               <span className="score mono">{p.score}</span>
//               {hasAnyAudio && p.start_time !== null && p.start_time !== undefined && (
//                 <button
//                   type="button"
//                   className="listen-btn"
//                   onClick={() => playEvidence(p.parameter, p.start_time, p.end_time)}
//                   title={`Play ${fmtTime(p.start_time)} - ${fmtTime(p.end_time)}`}
//                 >
//                   {nowPlaying?.parameter === p.parameter ? "▶ Playing" : "▶ Listen"}
//                 </button>
//               )}
//             </div>
//             {p.reason && (
//               <div style={{ fontSize: 12, color: "var(--gray)", padding: "0 0 8px 44px" }}>
//                 {p.reason}
//                 {p.evidence && (
//                   <span style={{ color: "var(--ink-soft)" }}> — "{p.evidence}"</span>
//                 )}
//               </div>
//             )}
//           </div>
//         ))}
//       </div>

//       <div className="summary-grid">
//         <div className="summary-card strength">
//           <h3>Strengths</h3>
//           <ul>{strengths.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
//         </div>
//         <div className="summary-card improve">
//           <h3>Improvement areas</h3>
//           <ul>{improvements.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
//         </div>
//       </div>

//       <div className="section-head" style={{ marginTop: 40 }}><h2>Transcript</h2></div>
//       {utterances.length > 0 ? (
//         <div className="param-list" style={{ padding: "4px 0" }}>
//           {utterances.map((u, i) => {
//             const isHighlighted =
//               nowPlaying &&
//               u.end >= nowPlaying.start_time &&
//               u.start <= (nowPlaying.end_time ?? nowPlaying.start_time + 0.01);
//             return (
//               <div
//                 key={i}
//                 className={`param-row ${isHighlighted ? "is-playing" : ""}`}
//                 style={{ gridTemplateColumns: "80px 60px 1fr" }}
//               >
//                 <span className="idx mono">{u.start?.toFixed ? u.start.toFixed(1) + "s" : u.start}</span>
//                 <span className="name" style={{ fontWeight: 600, color: u.speaker === "Agent" ? "var(--teal)" : "var(--ink)" }}>
//                   {u.speaker}
//                 </span>
//                 <span className="name">{u.text}</span>
//               </div>
//             );
//           })}
//         </div>
//       ) : (
//         <div className="state-msg">No transcript stored for this call.</div>
//       )}
//     </div>
//   );
// }
// import { useEffect, useRef, useState } from "react";
// import { api } from "../api";

// function barColor(score) {
//   if (score >= 80) return "var(--teal)";
//   if (score >= 60) return "var(--amber)";
//   return "var(--coral)";
// }

// function fmtTime(t) {
//   if (t === null || t === undefined) return "";
//   const m = Math.floor(t / 60);
//   const s = Math.floor(t % 60);
//   return `${m}:${String(s).padStart(2, "0")}`;
// }

// export default function CallDetail({ callId, onBack }) {
//   const [data, setData] = useState(null);
//   const [transcript, setTranscript] = useState(null);
//   const [error, setError] = useState("");
//   const [loading, setLoading] = useState(true);

//   // Which recording leg the "Listen" buttons control. Both legs share the
//   // same wall-clock timeline (call start = 0s), so a parameter's start_time
//   // seeks correctly on either one - the toggle just decides which audio you
//   // actually hear (agent voice vs. customer voice).
//   const [activeLeg, setActiveLeg] = useState("agent"); // 'agent' -> tx_path (OUT), 'customer' -> rx_path (IN)
//   const agentAudioRef = useRef(null);
//   const customerAudioRef = useRef(null);
//   const [nowPlaying, setNowPlaying] = useState(null); // parameter name currently being auditioned

//   useEffect(() => {
//     setLoading(true);
//     Promise.all([
//       api.callDetail(callId),
//       api.callTranscript(callId).catch(() => null),
//     ])
//       .then(([callData, tData]) => {
//         setData(callData);
//         setTranscript(tData);
//       })
//       .catch((err) => setError(err.message || "Could not load call"))
//       .finally(() => setLoading(false));
//   }, [callId]);

//   if (loading) return <div className="state-msg">Loading call scorecard…</div>;
//   if (error) return <div className="login-error">{error}</div>;
//   if (!data) return null;

//   const { call, parameters, fatal_checks } = data;
//   const triggeredFatal = fatal_checks.filter((f) => f.status);
//   const sorted = [...parameters].sort((a, b) => (b.score || 0) - (a.score || 0));
//   const strengths = sorted.slice(0, 3);
//   const improvements = sorted.slice(-3).reverse();
//   const hasAudio = Boolean(call.tx_path || call.rx_path);

//   let utterances = [];
//   try {
//     utterances = transcript ? JSON.parse(transcript.utterances_json) : [];
//   } catch {
//     utterances = [];
//   }

//   // Seek the chosen leg to this parameter's evidence timestamp and play it,
//   // pausing the other leg so audio doesn't overlap.
//   // function playEvidence(paramName, startTime) {
//   //   if (startTime === null || startTime === undefined) return;
//   //   const target = activeLeg === "agent" ? agentAudioRef.current : customerAudioRef.current;
//   //   const other = activeLeg === "agent" ? customerAudioRef.current : agentAudioRef.current;
//   //   if (!target) return;
//   //   if (other) other.pause();
//   //   target.currentTime = startTime;
//   //   target.play();
//   //   setNowPlaying(paramName);
//   // }

// function playEvidence(paramName, startTime, endTime) {
//   if (startTime === null || startTime === undefined) return;
//   const target = activeLeg === "agent" ? agentAudioRef.current : customerAudioRef.current;
//   const other = activeLeg === "agent" ? customerAudioRef.current : agentAudioRef.current;
//   if (!target || !target.src) {
//     setError("No recording available for this leg on this call.");
//     return;
//   }
//   if (other) other.pause();
//   target.currentTime = startTime;
//   target.play().catch((err) => setError(`Couldn't play recording: ${err.message}`));
//   setNowPlaying({ parameter: paramName, start_time: startTime, end_time: endTime });
// }

//   return (
//     <div>
//       <div className="breadcrumb">
//         <button onClick={onBack} type="button">Back</button>
//         <span>/</span>
//         <span>Call #{call.id}</span>
//         {console.log(call)}
//         <h2>clicked to get the detial of the call </h2>
//       </div>

//       <h1 className="page-title">{call.agent_name}</h1>
//       <p className="page-sub">
//         {call.call_service_name || "Call"} · {call.call_start_time || call.call_date}
//         {call.recording_base ? ` · ${call.recording_base}` : ""}
//       </p>

//       {call.fatal_error === 1 && (
//         <div className="login-error" style={{ marginBottom: 24 }}>
//           Fatal compliance flag raised on this call — review immediately.
//         </div>
//       )}

//       <div className="stat-grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
//         <div className="stat-card accent-ink">
//           <div className="label">Overall score</div>
//           <div className="value mono">{call.overall_score ?? "—"}</div>
//         </div>
//         <div className="stat-card accent-teal">
//           <div className="label">Verdict</div>
//           <div className="value mono" style={{ fontSize: 20 }}>{call.verdict || "—"}</div>
//         </div>
//         <div className="stat-card accent-amber">
//           <div className="label">Quality</div>
//           <div className="value mono" style={{ fontSize: 20 }}>{call.overall_quality || "—"}</div>
//         </div>
//         <div className={`stat-card ${triggeredFatal.length ? "accent-coral" : "accent-teal"}`}>
//           <div className="label">Fatal flags</div>
//           <div className="value mono">{triggeredFatal.length}</div>
//         </div>
//       </div>

//       {call.summary && (
//         <div className="summary-card" style={{ marginBottom: 32 }}>
//           <h3>Call summary</h3>
//           <p style={{ fontSize: 13, lineHeight: 1.7, margin: 0 }}>{call.summary}</p>
//         </div>
//       )}

//       {/* ---- Feature 2.3: call recording player ---- */}
//       {hasAudio && (
//         <div className="summary-card" style={{ marginBottom: 32 }}>
//           <h3>Call recording</h3>
//           <div className="leg-toggle">
//             <button
//               type="button"
//               className={activeLeg === "agent" ? "active" : ""}
//               onClick={() => setActiveLeg("agent")}
//               disabled={!call.tx_path}
//             >
//               Agent leg (OUT)
//             </button>
//             <button
//               type="button"
//               className={activeLeg === "customer" ? "active" : ""}
//               onClick={() => setActiveLeg("customer")}
//               disabled={!call.rx_path}
//             >
//               Customer leg (IN)
//             </button>
//           </div>
//           {call.tx_path && (
//             <audio
//               ref={agentAudioRef}
//               controls
//               src={call.tx_path}
//               style={{ width: "100%", marginTop: 10, display: activeLeg === "agent" ? "block" : "none" }}
//             />
//           )}
//           {call.rx_path && (
//             <audio
//               ref={customerAudioRef}
//               controls
//               src={call.rx_path}
//               style={{ width: "100%", marginTop: 10, display: activeLeg === "customer" ? "block" : "none" }}
//             />
//           )}
//           {/* <p className="hint" style={{ marginTop: 10 }}>
//             Use "Listen" next to any parameter below to jump straight to the moment that score was decided.
//           </p> */}
//         </div>
//       )}

//       <div className="section-head"><h2>Parameter-wise breakdown</h2></div>
//       <div className="param-list">
//         <h1>parameters value </h1>
//         {console.log("parameters", parameters)}
//         <p>***********************************</p>
//         {parameters.map((p, i) => (

//           <div className="param-row-detailed" key={p.parameter}>
//             <div className="param-row-main">
//               <span className="idx">{String(i + 1).padStart(2, "0")}</span>
//               <span className="name">{p.parameter}</span>
//               <span className="bar-track">
//                 <span className="bar-fill" style={{ width: `${p.raw_score}%`, background: barColor(p.raw_score) }} />
//               </span>
//               <span className="score mono">{p.raw_score}</span>
//               {hasAudio && p.start_time !== null && p.start_time !== undefined && (
//                 <button
//                   type="button"
//                   className="listen-btn"
//                   onClick={() => playEvidence(p.parameter, p.start_time)}
//                   title={`Play from ${fmtTime(p.start_time)}`}
//                 >
//                   {nowPlaying === p.parameter ? "▶ Playing" : "▶ Listen"}
//                 </button>
//               )}
//             </div>
//             {(p.reason || p.evidence) && (
//               <div className="param-row-evidence">
//                 {p.reason && <span>{p.reason}</span>}
//                 {p.evidence && (
//                   <span className="evidence-quote">
//                     "{p.evidence}"
//                     {p.start_time !== null && p.start_time !== undefined && (
//                       <span className="mono" style={{ color: "var(--gray)" }}> · {fmtTime(p.start_time)}</span>
//                     )}
//                   </span>
//                 )}
//               </div>
//             )}
//           </div>
//         ))}
//       </div>

//       <div className="summary-grid">
//         <div className="summary-card strength">
//           <h3>Strengths</h3>
//           <ul>{strengths.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
//         </div>
//         <div className="summary-card improve">
//           <h3>Improvement areas</h3>
//           <ul>{improvements.map((s) => <li key={s.parameter}>{s.parameter} — {s.reason}</li>)}</ul>
//         </div>
//       </div>

//       <div className="section-head" style={{ marginTop: 40 }}><h2>Transcript</h2></div>
//       {utterances.length > 0 ? (
//         <div className="param-list" style={{ padding: "4px 0" }}>
//           {utterances.map((u, i) => (
//             <div key={i} className="param-row" style={{ gridTemplateColumns: "80px 60px 1fr" }}>
//               <span className="idx mono">{u.start?.toFixed ? u.start.toFixed(1) + "s" : u.start}</span>
//               <span className="name" style={{ fontWeight: 600, color: u.speaker === "Agent" ? "var(--teal)" : "var(--ink)" }}>
//                 {u.speaker}
//               </span>
//               <span className="name">{u.text}</span>
//             </div>
//           ))}
//         </div>
//       ) : (
//         <div className="state-msg">No transcript stored for this call.</div>
//       )}
//     </div>
//   );
// }