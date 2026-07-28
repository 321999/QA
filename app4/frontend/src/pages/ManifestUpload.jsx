import { useEffect, useRef, useState } from "react";
import { api } from "../api";

const STATUS_LABEL = {
  pending: "Queued",
  transcribing: "Transcribing",
  analyzing: "Scoring",
  audited: "Audited",
  failed: "Failed",
};

function statusClass(status) {
  if (status === "audited") return "good";
  if (status === "failed") return "poor";
  return "avg";
}

export default function ManifestUpload({ onOpenCall }) {
  const [file, setFile] = useState(null);
  const [recordingBaseUrl, setRecordingBaseUrl] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [manifestId, setManifestId] = useState(null);
  const [manifest, setManifest] = useState(null);
  const [calls, setCalls] = useState([]);
  const [pastManifests, setPastManifests] = useState([]);
  const pollRef = useRef(null);

  useEffect(() => {
    api.listManifests().then((d) => setPastManifests(d.manifests)).catch(() => {});
  }, [manifestId]);

  useEffect(() => {
    if (!manifestId) return;
    async function poll() {
      try {
        const data = await api.manifestStatus(manifestId);
        setManifest(data.manifest);
        setCalls(data.calls);
        if (data.manifest.status === "processing" || data.manifest.status === "uploaded") {
          pollRef.current = setTimeout(poll, 2000);
        }
      } catch (err) {
        setError(err.message);
      }
    }
    poll();
    return () => clearTimeout(pollRef.current);
  }, [manifestId]);

  async function handleUpload(e) {
    e.preventDefault();
    if (!file) return;
    setUploading(true);
    setError("");
    try {
      const res = await api.uploadManifest(file, recordingBaseUrl || undefined);
      setManifestId(res.manifest_id);
    } catch (err) {
      setError(err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  function openPast(id) {
    setManifestId(id);
  }

  const progressPct = manifest && manifest.total_rows
    ? Math.round(((manifest.processed_rows + manifest.failed_rows) / manifest.total_rows) * 100)
    : 0;

  return (
    <div>
      <h1 className="page-title">Upload call manifest</h1>
      <p className="page-sub">
        Upload the Excel manifest exported from the dialer. Each row's IN/OUT recording legs
        are matched, sent to speech-to-text, then scored against the 16-point QA checklist —
        automatically, no manual audit needed.
      </p>

      <form onSubmit={handleUpload} className="summary-card" style={{ maxWidth: 560, marginBottom: 32 }}>
        <div className="field">
          <label htmlFor="manifest-file">Manifest file (.xlsx)</label>
          <input
            id="manifest-file"
            type="file"
            accept=".xlsx,.xlsm"
            onChange={(e) => setFile(e.target.files[0])}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="recording-base">Recordings base URL (optional override)</label>
          <input
            id="recording-base"
            type="text"
            placeholder="http://192.168.10.189/qa_upload"
            value={recordingBaseUrl}
            onChange={(e) => setRecordingBaseUrl(e.target.value)}
          />
        </div>
        {error && <div className="login-error">{error}</div>}
        <button className="btn-primary" type="submit" disabled={uploading || !file}>
          {uploading ? "Uploading…" : "Upload & process"}
        </button>
      </form>

      {manifest && (
        <>
          <div className="section-head">
            <h2>{manifest.filename}</h2>
            <span className="hint">
              {manifest.status === "processing" || manifest.status === "uploaded"
                ? `Processing… ${progressPct}%`
                : `${manifest.processed_rows} audited, ${manifest.failed_rows} failed`}
            </span>
          </div>

          <div className="agent-table" style={{ marginBottom: 40 }}>
            {calls.map((c) => (
              <button
                key={c.id}
                className="agent-row"
                type="button"
                onClick={() => c.status === "audited" && onOpenCall(c.id)}
                style={{ cursor: c.status === "audited" ? "pointer" : "default" }}
              >
                <span className="name">
                  {c.agent_name} <span style={{ color: "var(--gray)", fontWeight: 400 }}>· {c.recording_base || c.call_number || "—"}</span>
                </span>
                <span className="team">{c.verdict || (c.error_message ? "error" : "—")}</span>
                <span className="calls">{c.overall_score ?? "—"}</span>
                <span className={`score-chip ${statusClass(c.status)}`}>
                  {STATUS_LABEL[c.status] || c.status}
                </span>
              </button>
            ))}
            {calls.some((c) => c.status === "failed") && (
              <div style={{ padding: "12px 20px", fontSize: 12, color: "var(--gray)" }}>
                Failed rows keep their error message — check the recordings base URL and folder layout.
              </div>
            )}
          </div>
        </>
      )}

      {!manifest && pastManifests.length > 0 && (
        <>
          <div className="section-head"><h2>Previous uploads</h2></div>
          <div className="agent-table">
            {pastManifests.map((m) => (
              <button key={m.id} className="agent-row" type="button" onClick={() => openPast(m.id)}>
                <span className="name">{m.filename}</span>
                <span className="team">{m.total_rows} rows</span>
                <span className="calls">{new Date(m.uploaded_at).toLocaleDateString()}</span>
                <span className={`score-chip ${m.status === "done" ? "good" : m.status === "done_with_errors" ? "avg" : "poor"}`}>
                  {m.status}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
