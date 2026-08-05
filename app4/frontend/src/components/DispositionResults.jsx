// Disposition-wise Results - same click -> agents -> calls -> call detail
// flow as the 5 stat cards above, just scoped to a call-outcome ("Not
// Eligible" / "Not Interested" / "Callback" etc) instead of a status/sentiment
// bucket. Kept as its own component (not inlined in Dashboard.jsx) so the
// dashboard page stays a layout/data-fetching shell and this one section can
// be reused, tested, or restyled independently.
//
// "Correct" / "Wrong" are shown disabled - they depend on comparing the
// pipeline's predicted disposition against a ground-truth actual disposition
// the manifest doesn't capture yet, so they're wired into the UI now and
// ready to switch on the moment that data exists, per the spec ("implement
// later").
export default function DispositionResults({ dispositions, onSelectDisposition }) {
  return (
    <div className="disposition-results">
      <div className="section-head">
        <h2>Disposition-wise results</h2>
        {/* <span className="hint">Call outcome breakdown - tap to see which agents got each one</span> */}
      </div>

      <div className="disposition-row">
        {dispositions.map((d) => (
          <button
            key={d.category}
            type="button"
            className="disposition-pill"
            onClick={() => onSelectDisposition(d.category, d.label)}
            title={`See agents behind ${d.label.toLowerCase()}`}
          >
            <span className="disposition-count mono">{d.count}</span>
            <span className="disposition-label">{d.label}</span>
          </button>
        ))}

        <button
          type="button"
          className="disposition-pill disabled"
          disabled
          title="Coming soon - needs actual-vs-predicted disposition data"
        >
          <span className="disposition-count mono">—</span>
          <span className="disposition-label">Correct</span>
        </button>
        <button
          type="button"
          className="disposition-pill disabled"
          disabled
          title="Coming soon - needs actual-vs-predicted disposition data"
        >
          <span className="disposition-count mono">—</span>
          <span className="disposition-label">Wrong</span>
        </button>
      </div>
    </div>
  );
}
