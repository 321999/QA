function fmt(d) {
  return d.toISOString().slice(0, 10);
}

const PRESETS = [
  { label: "Last 7 days", days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "Last 90 days", days: 90 },
];

export default function DateRangePicker({ start, end, onChange }) {
  function applyPreset(days) {
    const e = new Date();
    const s = new Date();
    s.setDate(s.getDate() - days);
    onChange(fmt(s), fmt(e));
  }

  const activeDays = Math.round((new Date(end) - new Date(start)) / 86400000);

  return (
    <div className="range-row">
      <div className="quick-ranges">
        {PRESETS.map((p) => (
          <button
            key={p.days}
            className={activeDays === p.days ? "active" : ""}
            onClick={() => applyPreset(p.days)}
            type="button"
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="date-range">
        <input type="date" value={start} onChange={(e) => onChange(e.target.value, end)} />
        <span className="sep">to</span>
        <input type="date" value={end} onChange={(e) => onChange(start, e.target.value)} />
      </div>
    </div>
  );
}
