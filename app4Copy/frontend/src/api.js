const BASE = import.meta.env.VITE_API_URL || "http://localhost:8001";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

export const api = {
  login: (username, password) =>
    request("/api/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  summary: (start, end) => request(`/api/dashboard/summary?start=${start}&end=${end}`),
  parameters: (start, end) => request(`/api/dashboard/parameters?start=${start}&end=${end}`),
  agentsForParameter: (param, start, end) =>
    request(`/api/dashboard/parameters/${encodeURIComponent(param)}/agents?start=${start}&end=${end}`),
  agentDetail: (id, start, end) => request(`/api/agents/${id}?start=${start}&end=${end}`),
  agents: () => request(`/api/agents`),

  // report page: agent name / recording / marks per parameter, one row per audited call
  agentScoresReport: ({
    agentId, dispositionStatus, actualDisposition, predictedDisposition,
    start, end, page = 1, pageSize = 25, sort = "call_date_desc",
  }) => {
    const params = new URLSearchParams({ start, end, page, page_size: pageSize, sort });
    if (agentId) params.set("agent_id", agentId);
    if (dispositionStatus) params.set("disposition_status", dispositionStatus);
    if (actualDisposition) params.set("actual_disposition", actualDisposition);
    if (predictedDisposition) params.set("predicted_disposition", predictedDisposition);
    return request(`/api/reports/agent-scores?${params.toString()}`);
  },

  // distinct actual/predicted disposition values present in range, for the filter dropdowns
  reportDispositionOptions: (start, end) =>
    request(`/api/reports/disposition-options?start=${start}&end=${end}`),

  // Not a fetch - this just builds the URL for a plain <a href> download link,
  // using the exact same filter params as agentScoresReport above so the
  // download can never drift from what's currently on screen. The browser
  // handles the actual download via the server's Content-Disposition header.
  agentScoresReportExportUrl: ({
    agentId, dispositionStatus, actualDisposition, predictedDisposition, start, end, sort = "call_date_desc",
  }) => {
    const params = new URLSearchParams({ start, end, sort });
    if (agentId) params.set("agent_id", agentId);
    if (dispositionStatus) params.set("disposition_status", dispositionStatus);
    if (actualDisposition) params.set("actual_disposition", actualDisposition);
    if (predictedDisposition) params.set("predicted_disposition", predictedDisposition);
    return `${BASE}/api/reports/agent-scores/export?${params.toString()}`;
  },

  // feature 1: parameter success-rate funnel (sorted highest-first server-side)
  parameterFunnel: (start, end) => request(`/api/dashboard/parameter-funnel?start=${start}&end=${end}`),

  // disposition counts (Not Eligible / Not Interested / Callback), drill-down
  // reuses the same categoryAgents/categoryAgentCalls calls below.
  dispositions: (start, end) => request(`/api/dashboard/dispositions?start=${start}&end=${end}`),

  // feature 2: clickable stat cards -> agents -> that agent's calls
  categoryAgents: (category, start, end) =>
    request(`/api/dashboard/category/${category}/agents?start=${start}&end=${end}`),
  categoryAgentCalls: (category, agentId, start, end) =>
    request(`/api/dashboard/category/${category}/agents/${agentId}/calls?start=${start}&end=${end}`),

  // feature 3: top-10 leaderboard sidebar
  topAgents: (start, end, limit = 10, search) => {
    const params = new URLSearchParams({ start, end, limit });
    if (search) params.set("search", search);
    return request(`/api/dashboard/top-agents?${params.toString()}`);
  },

  // manifest upload + STT/SLM pipeline
  uploadManifest: async (file, recordingBaseUrl) => {
    const form = new FormData();
    form.append("file", file);
    let url = "/api/manifest/upload?auto_process=true";
    if (recordingBaseUrl) url += `&recording_base_url=${encodeURIComponent(recordingBaseUrl)}`;
    const res = await fetch(`${BASE}${url}`, { method: "POST", body: form });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Upload failed: ${res.status}`);
    }
    return res.json();
  },
  listManifests: () => request("/api/manifest"),
  manifestStatus: (id) => request(`/api/manifest/${id}`),
  reprocessManifest: (id) => request(`/api/manifest/${id}/process`, { method: "POST" }),
  callDetail: (id) => request(`/api/calls/${id}`),
  callTranscript: (id) => request(`/api/calls/${id}/transcript`),
};

// const BASE = import.meta.env.VITE_API_URL || "http://localhost:8001";

// async function request(path, options = {}) {
//   const res = await fetch(`${BASE}${path}`, {
//     headers: { "Content-Type": "application/json" },
//     ...options,
//   });
//   if (!res.ok) {
//     const body = await res.json().catch(() => ({}));
//     throw new Error(body.detail || `Request failed: ${res.status}`);
//   }
//   return res.json();
// }

// export const api = {
//   login: (username, password) =>
//     request("/api/login", { method: "POST", body: JSON.stringify({ username, password }) }),
//   summary: (start, end) => request(`/api/dashboard/summary?start=${start}&end=${end}`),
//   parameters: (start, end) => request(`/api/dashboard/parameters?start=${start}&end=${end}`),
//   agentsForParameter: (param, start, end) =>
//     request(`/api/dashboard/parameters/${encodeURIComponent(param)}/agents?start=${start}&end=${end}`),
//   agentDetail: (id, start, end) => request(`/api/agents/${id}?start=${start}&end=${end}`),
//   agents: () => request(`/api/agents`),

//   // report page: agent name / recording / marks per parameter, one row per audited call
//   agentScoresReport: ({ agentId, start, end, page = 1, pageSize = 25, sort = "call_date_desc" }) => {
//     const params = new URLSearchParams({ start, end, page, page_size: pageSize, sort });
//     if (agentId) params.set("agent_id", agentId);
//     return request(`/api/reports/agent-scores?${params.toString()}`);
//   },

//   // feature 1: parameter success-rate funnel (sorted highest-first server-side)
//   parameterFunnel: (start, end) => request(`/api/dashboard/parameter-funnel?start=${start}&end=${end}`),

//   // disposition counts (Not Eligible / Not Interested / Callback), drill-down
//   // reuses the same categoryAgents/categoryAgentCalls calls below.
//   dispositions: (start, end) => request(`/api/dashboard/dispositions?start=${start}&end=${end}`),

//   // feature 2: clickable stat cards -> agents -> that agent's calls
//   categoryAgents: (category, start, end) =>
//     request(`/api/dashboard/category/${category}/agents?start=${start}&end=${end}`),
//   categoryAgentCalls: (category, agentId, start, end) =>
//     request(`/api/dashboard/category/${category}/agents/${agentId}/calls?start=${start}&end=${end}`),

//   // feature 3: top-10 leaderboard sidebar
//   topAgents: (start, end, limit = 10, search) => {
//     const params = new URLSearchParams({ start, end, limit });
//     if (search) params.set("search", search);
//     return request(`/api/dashboard/top-agents?${params.toString()}`);
//   },

//  //   // change the password 
//   changePassword: (username, currentPassword, newPassword) =>
//   request("/api/change-password", {
//     method: "POST",
//     body: JSON.stringify({ username, current_password: currentPassword, new_password: newPassword }),
//   }),
//   // manifest upload + STT/SLM pipeline
//   uploadManifest: async (file, recordingBaseUrl) => {
//     const form = new FormData();
//     form.append("file", file);
//     let url = "/api/manifest/upload?auto_process=true";
//     if (recordingBaseUrl) url += `&recording_base_url=${encodeURIComponent(recordingBaseUrl)}`;
//     const res = await fetch(`${BASE}${url}`, { method: "POST", body: form });
//     if (!res.ok) {
//       const body = await res.json().catch(() => ({}));
//       throw new Error(body.detail || `Upload failed: ${res.status}`);
//     }
//     return res.json();
//   },
//   listManifests: () => request("/api/manifest"),
//   manifestStatus: (id) => request(`/api/manifest/${id}`),
//   reprocessManifest: (id) => request(`/api/manifest/${id}/process`, { method: "POST" }),
//   callDetail: (id) => request(`/api/calls/${id}`),
//   callTranscript: (id) => request(`/api/calls/${id}/transcript`),
// };
