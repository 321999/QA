const BASE = import.meta.env.VITE_API_URL || "http://localhost:8001";

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

//   // feature 1: parameter success-rate funnel (sorted highest-first server-side)
//   parameterFunnel: (start, end) => request(`/api/dashboard/parameter-funnel?start=${start}&end=${end}`),

//   // feature 2: clickable stat cards -> agents -> that agent's calls
//   categoryAgents: (category, start, end) =>
//     request(`/api/dashboard/category/${category}/agents?start=${start}&end=${end}`),
//   categoryAgentCalls: (category, agentId, start, end) =>
//     request(`/api/dashboard/category/${category}/agents/${agentId}/calls?start=${start}&end=${end}`),

//   // feature 3: top-10 leaderboard sidebar
//   topAgents: (start, end, limit = 10) =>
//     request(`/api/dashboard/top-agents?start=${start}&end=${end}&limit=${limit}`),

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

//   // change the password 
//   changePassword: (username, currentPassword, newPassword) =>
//   request("/api/change-password", {
//     method: "POST",
//     body: JSON.stringify({ username, current_password: currentPassword, new_password: newPassword }),
//   }),
// };
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
  topAgents: (start, end, limit = 10) =>
    request(`/api/dashboard/top-agents?start=${start}&end=${end}&limit=${limit}`),

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
