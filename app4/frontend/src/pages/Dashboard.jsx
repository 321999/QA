// import { useEffect, useState, useCallback } from "react";
// import { api } from "../api";
// import DateRangePicker from "../components/DateRangePicker";
// import TopAgentsBox from "../components/TopAgentsBox";

// function fmt(d) {
//   return d.toISOString().slice(0, 10);
// }

// function defaultRange() {
//   const e = new Date();
//   const s = new Date();
//   s.setDate(s.getDate() - 30);
//   return { start: fmt(s), end: fmt(e) };
// }

// function barColor(pct) {
//   if (pct >= 80) return "var(--teal)";
//   if (pct >= 60) return "var(--amber)";
//   return "var(--coral)";
// }

// // Feature 2: the 5 top stat cards are all clickable. Each maps to a
// // `category` key the backend's /api/dashboard/category/{category}/... routes
// // understand (see CATEGORY_FILTERS in main.py).
// const CARD_DEFS = [
//   { key: "total", label: "Total calls", field: "total_calls", accent: "ink" },
//   { key: "audited", label: "Audited calls", field: "audited_calls", accent: "teal" },
//   { key: "pending", label: "Pending calls", field: "pending_calls", accent: "amber" },
//   { key: "positive", label: "Positive calls", field: "positive_calls", accent: "teal" },
//   { key: "negative", label: "Negative calls", field: "negative_calls", accent: "coral" },
// ];

// export default function Dashboard({ onSelectParameter, onSelectCategory, onSelectAgent }) {
//   const [{ start, end }, setRange] = useState(defaultRange());
//   const [summary, setSummary] = useState(null);
//   const [funnel, setFunnel] = useState([]);
//   const [topAgents, setTopAgents] = useState([]);
//   const [loading, setLoading] = useState(true);
//   const [error, setError] = useState("");

//   const load = useCallback(async (s, e) => {
//     setLoading(true);
//     setError("");
//     try {
//       // Everything the dashboard needs, fetched together: top cards,
//       // feature 1 (parameter success-rate funnel), feature 3 (leaderboard).
//       const [sum, funnelData, topData] = await Promise.all([
//         api.summary(s, e),
//         api.parameterFunnel(s, e),
//         api.topAgents(s, e, 10),
//       ]);
//       setSummary(sum);
//       setFunnel(funnelData.parameters);
//       setTopAgents(topData.agents);
//     } catch (err) {
//       setError(err.message || "Could not load dashboard");
//     } finally {
//       setLoading(false);
//     }
//   }, []);

//   useEffect(() => {
//     load(start, end);
//   }, [start, end, load]);

//   const cards = summary
//     ? CARD_DEFS.map((c) => ({ ...c, value: summary[c.field] }))
//     : [];

//   return (
//     <div>
//       <h1 className="page-title">Audit overview</h1>
//       <p className="page-sub">Call quality performance across the floor, {start} to {end}.</p>

//       <DateRangePicker start={start} end={end} onChange={(s, e) => setRange({ start: s, end: e })} />

//       {error && <div className="login-error">{error}</div>}

//       {loading ? (
//         <div className="state-msg">Loading audit data…</div>
//       ) : (
//         <div className="dashboard-layout">
//           {/* ---- main column: stat cards + funnel ---- */}
//           <div className="dashboard-main">
//             <div className="stat-grid">
//               {cards.map((c) => (
//                 <button
//                   key={c.label}
//                   className={`stat-card accent-${c.accent} clickable`}
//                   type="button"
//                   onClick={() => onSelectCategory(c.key, c.label, start, end)}
//                   title={`See agents behind ${c.label.toLowerCase()}`}
//                 >
//                   <div className="label">{c.label}</div>
//                   <div className="value mono">{c.value}</div>
//                 </button>
//               ))}
//             </div>

//             <div className="section-head">
//               <h2>Parameter success funnel</h2>
//               <span className="hint">
//                 % of audited calls that passed each checklist item — highest first
//               </span>
//             </div>

//             <div className="param-list">
//               {funnel.map((p, i) => (
//                 <button
//                   key={p.parameter}
//                   className="param-row"
//                   onClick={() => onSelectParameter(p.parameter, start, end)}
//                   type="button"
//                   title={`${p.sample_size} calls scored`}
//                 >
//                   <span className="idx">{String(i + 1).padStart(2, "0")}</span>
//                   <span className="name">{p.parameter}</span>
//                   <span className="bar-track">
//                     <span
//                       className="bar-fill"
//                       style={{ width: `${p.success_pct}%`, background: barColor(p.success_pct) }}
//                     />
//                   </span>
//                   <span className="score mono">{p.success_pct}%</span>
//                 </button>
//               ))}
//               {funnel.length === 0 && (
//                 <div className="state-msg">No audited calls in this date range yet.</div>
//               )}
//             </div>
//           </div>

//           {/* ---- feature 3: top-10 leaderboard sidebar ---- */}
//           <TopAgentsBox
//             agents={topAgents}
//             onSelectAgent={(agentId) => onSelectAgent(agentId, start, end)}
//           />
//         </div>
//       )}
//     </div>
//   );
// }




// **************************************w1***********************************************
// import { useEffect, useState, useCallback } from "react";
// import { api } from "../api";
// import DateRangePicker from "../components/DateRangePicker";
// import FunnelChart from "../components/FunnelChart";
// import TopAgentsBox from "../components/TopAgentsBox";
// import DispositionResults from "../components/DispositionResults";
// // function fmt(d) {
// //   return d.toISOString().slice(0, 10);
// // }

// // function defaultRange() {
// //   const e = new Date();
// //   const s = new Date();
// //   s.setDate(s.getDate() - 30);
// //   return { start: fmt(s), end: fmt(e) };
// // }

// // Feature 2: the 5 top stat cards are all clickable. Each maps to a
// // `category` key the backend's /api/dashboard/category/{category}/... routes
// // understand (see CATEGORY_FILTERS in main.py).
// const CARD_DEFS = [
//   { key: "total", label: "Total calls", field: "total_calls", accent: "ink" },
//   { key: "audited", label: "Audited calls", field: "audited_calls", accent: "teal" },
//   { key: "pending", label: "Pending calls", field: "pending_calls", accent: "amber" },
//   { key: "positive", label: "Positive calls", field: "positive_calls", accent: "teal" },
//   { key: "negative", label: "Negative calls", field: "negative_calls", accent: "coral" },
// ];

// export default function Dashboard({ start,end,onSelectParameter, onSelectCategory, onSelectAgent }) {
//   // const [{ start, end }, setRange] = useState(defaultRange());
//   const [summary, setSummary] = useState(null);
//   const [funnel, setFunnel] = useState([]);
//   const [totalAudited, setTotalAudited] = useState(0);
//   const [topAgents, setTopAgents] = useState([]);
//   const [loading, setLoading] = useState(true);
//   const [error, setError] = useState("");

//   const load = useCallback(async (s, e) => {
//     setLoading(true);
//     setError("");
//     try {
//       // Everything the dashboard needs, fetched together: top cards,
//       // feature 1 (parameter success-rate funnel), feature 3 (leaderboard).
//       const [sum, funnelData, topData] = await Promise.all([
//         api.summary(s, e),
//         api.parameterFunnel(s, e),
//         api.topAgents(s, e, 10),
//       ]);
//       setSummary(sum);
//       setFunnel(funnelData.parameters);
//       setTotalAudited(funnelData.total_audited_calls);
//       setTopAgents(topData.agents);
//     } catch (err) {
//       setError(err.message || "Could not load dashboard");
//     } finally {
//       setLoading(false);
//     }
//   }, []);

//   useEffect(() => {
//     load(start, end);
//   }, [start, end, load]);

//   const cards = summary
//     ? CARD_DEFS.map((c) => ({ ...c, value: summary[c.field] }))
//     : [];

//   return (
//     <div className="dashboard-viewport">
//       {/* <h1 className="page-title">Audit overview</h1>
//       <p className="page-sub">Call quality performance across the floor, {start} to {end}.</p> */}

//       {/* <DateRangePicker start={start} end={end} onChange={(s, e) => setRange({ start: s, end: e })} /> */}

//       {error && <div className="login-error">{error}</div>}

//       {loading ? (
//         <div className="state-msg">Loading audit data…</div>
//       ) : (
//         <div className="dashboard-layout">
//           {/* ---- main column: stat cards + funnel ---- */}
//           <div className="dashboard-main">
//             <div className="stat-grid">
//               {cards.map((c) => (
//                 <button
//                   key={c.label}
//                   className={`stat-card accent-${c.accent} clickable`}
//                   type="button"
//                   onClick={() => onSelectCategory(c.key, c.label, start, end)}
//                   title={`See agents behind ${c.label.toLowerCase()}`}
//                 >
//                   <div className="label">{c.label}</div>
//                   <div className="value mono">{c.value}</div>
//                 </button>
//               ))}
//             </div>

//                {/* <DispositionResults
//             dispositions={dispositions}
//             onSelectDisposition={(category, label) => onSelectCategory(category, label, start, end)}
//           /> */}
//             <div className="section-head">
//                <DispositionResults
//             dispositions={dispositions}
//             onSelectDisposition={(category, label) => onSelectCategory(category, label, start, end)}
//           />
//               <h2>Parameter success funnel</h2>
//               {/* <span className="hint">
//                 % of audited calls that passed each checklist item — highest first
//               </span> */}
//             </div>

//             <FunnelChart
//               data={funnel}
//               totalAudited={totalAudited}
//               onSelectParameter={(parameter) => onSelectParameter(parameter, start, end)}
//             />
//           </div>

//           {/* ---- feature 3: top-10 leaderboard sidebar ---- */}
//           <TopAgentsBox
//             agents={topAgents}
//             onSelectAgent={(agentId) => onSelectAgent(agentId, start, end)}
//           />
//         </div>
//       )}
//     </div>
//   );
// }


// *************************************** w2 *******************************************
import { useEffect, useState, useCallback } from "react";
import { api } from "../api";
import FunnelChart from "../components/FunnelChart";
import TopAgentsBox from "../components/TopAgentsBox";
import DispositionResults from "../components/DispositionResults";

// Feature 2: the 5 top stat cards are all clickable. Each maps to a
// `category` key the backend's /api/dashboard/category/{category}/... routes
// understand (see CATEGORY_FILTERS in main.py).
const CARD_DEFS = [
  { key: "total", label: "Total calls", field: "total_calls", accent: "ink" },
  { key: "audited", label: "Audited calls", field: "audited_calls", accent: "teal" },
  { key: "pending", label: "Pending calls", field: "pending_calls", accent: "amber" },
  { key: "positive", label: "Positive calls", field: "positive_calls", accent: "teal" },
  { key: "negative", label: "Negative calls", field: "negative_calls", accent: "coral" },
];

export default function Dashboard({ start, end, onSelectParameter, onSelectCategory, onSelectAgent }) {
  const [summary, setSummary] = useState(null);
  const [funnel, setFunnel] = useState([]);
  const [totalAudited, setTotalAudited] = useState(0);
  const [topAgents, setTopAgents] = useState([]);
  const [dispositions, setDispositions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (s, e) => {
    setLoading(true);
    setError("");
    try {
      const [sum, funnelData, topData, dispData] = await Promise.all([
        api.summary(s, e),
        api.parameterFunnel(s, e),
        api.topAgents(s, e, 10),
        api.dispositions(s, e),
      ]);
      setSummary(sum);
      setFunnel(funnelData.parameters);
      setTotalAudited(funnelData.total_audited_calls);
      setTopAgents(topData.agents);
      setDispositions(dispData.dispositions);
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
    ? CARD_DEFS.map((c) => ({ ...c, value: summary[c.field] }))
    : [];

  if (loading) {
    return <div className="state-msg">Loading audit data…</div>;
  }

  return (
    <div className="dashboard-viewport">
      {error && <div className="login-error">{error}</div>}

      <div className="dashboard-layout">
        <div className="dashboard-main">
          <div className="stat-grid">
            {cards.map((c) => (
              <button
                key={c.label}
                className={`stat-card accent-${c.accent} clickable`}
                type="button"
                onClick={() => onSelectCategory(c.key, c.label, start, end)}
                title={`See agents behind ${c.label.toLowerCase()}`}
              >
                <div className="label">{c.label}</div>
                <div className="value mono">{c.value}</div>
              </button>
            ))}
          </div>

{/* for the disposition wise */}
          <DispositionResults
            dispositions={dispositions}
            onSelectDisposition={(category, label) => onSelectCategory(category, label, start, end)}
          />

          <div className="section-head">
            <h2>Parameter success funnel</h2>
            {/* <span className="hint">
              % of audited calls that passed each checklist item — highest first
            </span> */}
          </div>

          <FunnelChart
            data={funnel}
            totalAudited={totalAudited}
            onSelectParameter={(parameter) => onSelectParameter(parameter, start, end)}
          />
        </div>

        <TopAgentsBox
          agents={topAgents}
          onSelectAgent={(agentId) => onSelectAgent(agentId, start, end)}
        />
      </div>
    </div>
  );
}
