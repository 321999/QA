import { useEffect, useState, useCallback } from "react";
import { api } from "../api";
import DateRangePicker from "../components/DateRangePicker";

function fmt(d) {
  return d.toISOString().slice(0, 10);
}

function defaultRange() {
  const e = new Date();
  const s = new Date();
  s.setDate(s.getDate() - 30);
  return { start: fmt(s), end: fmt(e) };
}

function barColor(score) {
  if (score >= 80) return "var(--teal)";
  if (score >= 60) return "var(--amber)";
  return "var(--coral)";
}

export default function Dashboard({ onSelectParameter }) {
  const [{ start, end }, setRange] = useState(defaultRange());
  const [summary, setSummary] = useState(null);
  const [parameters, setParameters] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (s, e) => {
    setLoading(true);
    setError("");
    try {
      const [sum, params] = await Promise.all([api.summary(s, e), api.parameters(s, e)]);
      setSummary(sum);
      setParameters(params.parameters);
    } catch (err) {
      setError(err.message || "Could not load dashboard");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(start, end);
  }, [start, end, load]);

  const cards = summary
    ? [
        { label: "Total calls", value: summary.total_calls, accent: "ink" },
        { label: "Audited calls", value: summary.audited_calls, accent: "teal" },
        { label: "Pending calls", value: summary.pending_calls, accent: "amber" },
        { label: "Positive calls", value: summary.positive_calls, accent: "teal" },
        { label: "Negative calls", value: summary.negative_calls, accent: "coral" },
      ]
    : [];

  return (
    <div>
      <h1 className="page-title">Audit overview</h1>
      <p className="page-sub">Call quality performance across the floor, {start} to {end}.</p>

      <DateRangePicker start={start} end={end} onChange={(s, e) => setRange({ start: s, end: e })} />

      {error && <div className="login-error">{error}</div>}

      {loading ? (
        <div className="state-msg">Loading audit data…</div>
      ) : (
        <>
          <div className="stat-grid">
            {cards.map((c) => (
              <div key={c.label} className={`stat-card accent-${c.accent}`}>
                <div className="label">{c.label}</div>
                <div className="value mono">{c.value}</div>
              </div>
            ))}
          </div>

          <div className="section-head">
            <h2>QA checklist performance</h2>
            <span className="hint">Tap a parameter to see the agents behind that score</span>
          </div>

          <div className="param-list">
            {parameters.map((p, i) => (
              <button
                key={p.parameter}
                className="param-row"
                onClick={() => onSelectParameter(p.parameter, start, end)}
                type="button"
              >
                <span className="idx">{String(i + 1).padStart(2, "0")}</span>
                <span className="name">{p.parameter}</span>
                <span className="bar-track">
                  <span
                    className="bar-fill"
                    style={{
                      width: `${p.avg_score}%`,
                      background: barColor(p.avg_score),
                    }}
                  />
                </span>
                <span className="score mono">{p.avg_score}</span>
              </button>
            ))}
            {parameters.length === 0 && (
              <div className="state-msg">No audited calls in this date range yet.</div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
