import React, { useEffect, useState } from "react";
import "./FunnelChart.css";

// Red -> Orange -> Yellow -> Green -> Teal -> Blue -> Navy
const STOPS = [
  [230, 57, 50],
  [235, 140, 40],
  [225, 190, 50],
  [120, 185, 75],
  [50, 165, 150],
  [40, 100, 160],
  [25, 50, 95],
];

function colorAt(t) {
  const seg = t * (STOPS.length - 1);
  const i = Math.min(Math.floor(seg), STOPS.length - 2);
  const localT = seg - i;
  const [r1, g1, b1] = STOPS[i];
  const [r2, g2, b2] = STOPS[i + 1];
  const r = Math.round(r1 + (r2 - r1) * localT);
  const g = Math.round(g1 + (g2 - g1) * localT);
  const b = Math.round(b1 + (b2 - b1) * localT);
  return `rgb(${r},${g},${b})`;
}

const BASE_HEIGHT = 190;
const VIEW_W = 40; // fixed viewBox width - never changes, so the curve math never distorts it was at 56(which is good)
const VIEW_H = BASE_HEIGHT; // fixed viewBox height - same reason

export default function FunnelChart({ data, totalAudited, onSelectParameter }) {
  const [grown, setGrown] = useState(false);
  const [hoverIdx, setHoverIdx] = useState(null);

  // Grow the funnel once per new dataset. Two rAFs guarantee the browser has
  // painted the 0-scale state first, so the transition actually animates
  // instead of snapping straight to full height.
  useEffect(() => {
    setGrown(false);
    const t = requestAnimationFrame(() => requestAnimationFrame(() => setGrown(true)));
    return () => cancelAnimationFrame(t);
  }, [data]);

  if (!data || data.length === 0) {
    return <div className="state-msg">No audited calls in this range yet.</div>;
  }

  const n = data.length;
  const curveOffset = 7;
  const innerShadowDepth = 8;

  return (
    <div className="funnel-container">
      <div className="funnel-wrapper">
        <div className="funnel-track">
          {data.map((row, i) => {
            // Strictly decreasing by RANK (not raw %), so the shape is always
            // a clean cone regardless of how close the actual percentages
            // are to each other - #1 is always full height, the last item
            // is always 25% height, everything else spaced evenly between.
            const taperFactor = 1 - (i / (n - 1 || 1)) * 0.75;
            const color = colorAt(i / (n - 1 || 1));

            return (
              <div
                key={row.parameter_id ?? i}
                className="funnel-col"
                onMouseEnter={() => setHoverIdx(i)}
                onMouseLeave={() => setHoverIdx(null)}
                onClick={() => onSelectParameter && onSelectParameter(row.parameter)}
              >
                <div className="funnel-segment-wrap" style={{ height: `${BASE_HEIGHT}px` }}>
                  {/* The svg box itself NEVER resizes - only its content is
                      scaled via CSS transform. That's what makes growth (and
                      the hover lift) purely visual with zero layout impact,
                      so nothing around it can ever jitter/reflow. */}
                  <svg
                    viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
                    className="funnel-3d-svg"
                    style={{
                      transform: `scaleY(${grown ? taperFactor : 0})`,
                      "--taper": taperFactor,
                      transitionDelay: `${i * 35}ms`,
                    }}
                  >
                    <path
                      d={`M ${innerShadowDepth} 0
                         Q 0 ${VIEW_H / 2} ${innerShadowDepth} ${VIEW_H}
                         L 0 ${VIEW_H}
                         Q 0 ${VIEW_H / 2} 0 0 Z`}
                      fill="#1a1a1a"
                    />
                    <path
                      d={`M ${innerShadowDepth} 0
                         Q ${innerShadowDepth - curveOffset} ${VIEW_H / 2} ${innerShadowDepth} ${VIEW_H}
                         L ${VIEW_W} ${VIEW_H - 6}
                         Q ${VIEW_W + curveOffset} ${VIEW_H / 2} ${VIEW_W} 6 Z`}
                      fill={color}
                    />
                  </svg>

                  {grown && (
                    <div
                      className="panel-overlay"
                      style={{ bottom: `${(1 - taperFactor) * BASE_HEIGHT}px`, height: `${BASE_HEIGHT * taperFactor}px` }}
                    >
                      <span className="panel-pct">{row.success_pct}%</span>
                      <div className="panel-icon">
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none">
                          <circle cx="12" cy="12" r="9" stroke="white" strokeWidth="2" />
                          <path d="M9 12l2 2 4-4" stroke="white" strokeWidth="2" strokeLinecap="round" />
                        </svg>
                      </div>
                    </div>
                  )}
                </div>

                {hoverIdx === i && (
                  <div className={`funnel-tooltip ${i < n / 2 ? "tt-below" : "tt-above"}`}>
                    <strong>{row.success_pct}%</strong> 
                    {/* ({row.success_count || 0} / {totalAudited || 0} calls) */}
                    <br />
                    <small>{row.parameter}</small>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}