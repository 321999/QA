import React, { useEffect, useState } from "react";
import "./FunnelChart.css";

// Updated color ramp matching the prompt image: Red -> Orange -> Yellow -> Green -> Teal -> Blue
const STOPS = [
  [230, 57, 50],   // Red
  [235, 140, 40],  // Orange
  [225, 190, 50],  // Yellow
  [120, 185, 75],  // Lime Green
  [50, 165, 150],  // Teal
  [40, 100, 160],  // Sky Blue
  [25, 50, 95]     // Navy
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

export default function FunnelChart({ data, totalAudited, onSelectParameter }) {
  const [animated, setAnimated] = useState(false);
  const [hoverIdx, setHoverIdx] = useState(null);

  useEffect(() => {
    setAnimated(false);
    const t = requestAnimationFrame(() => requestAnimationFrame(() => setAnimated(true)));
    return () => cancelAnimationFrame(t);
  }, [data]);

  if (!data || data.length === 0) {
    return <div className="state-msg">No audited calls in this range yet.</div>;
  }

  const n = data.length;
  // Outer box fixed height reference
  const BASE_HEIGHT = 160; 

  return (
    <div className="funnel-container">
      {/* <div className="funnel-header">
         <div className="title-section">
        //   <h2>Parameter success funnel</h2>
        //   <span className="subtitle">% of audited calls that passed each checklist item — highest first</span>
        // </div> 
      </div>  */}

      <div className="funnel-wrapper">
        {/* <div className="axis-label left">Initial Metrics</div> */}

        <div className="funnel-track">
          {data.map((row, i) => {
            // Tapering height calculation for perspective (Decreasing left to right)
            const taperFactor = 1 - (i / (n - 1 || 1)) * 0.45; // Starts at 100% size down to 55%
            const h = animated ? BASE_HEIGHT * taperFactor : 0;
            const w = 48; // Width of each panel segment
            const color = colorAt(i / (n - 1 || 1));
            const labelAbove = i % 2 === 0;

            // SVG Path Calculations for 3D Lens/Curved Segment
            const curveOffset = 7; // Outward curve amount
            const innerShadowDepth = 8; // Depth of left dark rim

            return (
              <div
                key={row.parameter_id || i}
                className="funnel-col"
                onMouseEnter={() => setHoverIdx(i)}
                onMouseLeave={() => setHoverIdx(null)}
                onClick={() => onSelectParameter && onSelectParameter(row.parameter)}
              >
                {/* Text Above */}
                <div className={`funnel-label label-above ${labelAbove ? "visible" : "hidden"}`}>
                  {row.parameter}
                </div>

                {/* 3D Segment */}
                <div className="funnel-segment-wrap" style={{ height: `${BASE_HEIGHT}px` }}>
                  <svg
                    viewBox={`0 0 ${w + innerShadowDepth} ${BASE_HEIGHT}`}
                    style={{
                      height: `${h}px`,
                      transition: `height 0.6s cubic-bezier(0.16, 1, 0.3, 1) ${i * 40}ms`
                    }}
                    className="funnel-3d-svg"
                  >
                    {/* Dark inner shadow edge on left */}
                    <path
                      d={`M ${innerShadowDepth} 0 
                         Q 0 ${BASE_HEIGHT / 2} ${innerShadowDepth} ${BASE_HEIGHT} 
                         L 0 ${BASE_HEIGHT} 
                         Q 0 ${BASE_HEIGHT / 2} 0 0 Z`}
                      fill="#1a1a1a"
                    />

                    {/* Main Curved Colored Lens Face */}
                    <path
                      d={`M ${innerShadowDepth} 0 
                         Q ${innerShadowDepth - curveOffset} ${BASE_HEIGHT / 2} ${innerShadowDepth} ${BASE_HEIGHT} 
                         L ${w + innerShadowDepth} ${BASE_HEIGHT - 6} 
                         Q ${w + innerShadowDepth + curveOffset} ${BASE_HEIGHT / 2} ${w + innerShadowDepth} 6 Z`}
                      fill={color}
                    />
                  </svg>

                  {/* Percentage Content & Icon on the Panel */}
                  {animated && (
                    <div className="panel-overlay">
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

                {/* Text Below */}
                <div className={`funnel-label label-below ${!labelAbove ? "visible" : "hidden"}`}>
                  {row.parameter}
                </div>

                {/* Tooltip */}
                {hoverIdx === i && (
                  <div className={`funnel-tooltip ${labelAbove ? "tt-below" : "tt-above"}`}>
                    <strong>{row.success_pct}%</strong> ({row.success_count || 0} / {totalAudited || 0} calls)
                    <br />
                    <small>{row.parameter}</small>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* <div className="axis-label right">Final Metrics</div> */}
      </div>
    </div>
  );
}