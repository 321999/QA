import { useState } from "react";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import ParameterAgents from "./pages/ParameterAgents";
import AgentDetail from "./pages/AgentDetail";
import ManifestUpload from "./pages/ManifestUpload";
import CallDetail from "./pages/CallDetail";

export default function App() {
  const [session, setSession] = useState(null);
  // view.name: 'dashboard' | 'parameterAgents' | 'agentDetail' | 'manifestUpload' | 'callDetail'
  const [view, setView] = useState({ name: "dashboard" });

  if (!session) {
    return <Login onLogin={setSession} />;
  }

  function goDashboard() { setView({ name: "dashboard" }); }
  function goParameterAgents(parameter, start, end) { setView({ name: "parameterAgents", parameter, start, end }); }
  function goAgentDetail(agentId, start, end) { setView({ name: "agentDetail", agentId, start, end }); }
  function goManifestUpload() { setView({ name: "manifestUpload" }); }
  function goCallDetail(callId, back) { setView({ name: "callDetail", callId, back: back || { name: "manifestUpload" } }); }

  function logout() {
    setSession(null);
    setView({ name: "dashboard" });
  }

  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="brand"><span className="dot" />Athena QA </div>
        <div className="topbar-right">
          <button
            className="logout-btn"
            type="button"
            onClick={goManifestUpload}
            style={{ background: view.name === "manifestUpload" ? "rgba(255,255,255,0.12)" : "transparent" }}
          >
            Upload manifest
          </button>
          <button className="logout-btn" type="button" onClick={goDashboard}>Dashboard</button>
          <span>{session.username}</span>
          <button className="logout-btn" onClick={logout} type="button">Sign out</button>
        </div>
      </div>

      <div className="content">
        {view.name === "dashboard" && <Dashboard onSelectParameter={goParameterAgents} />}
        {view.name === "parameterAgents" && (
          <ParameterAgents
            parameter={view.parameter}
            start={view.start}
            end={view.end}
            onBack={goDashboard}
            onSelectAgent={goAgentDetail}
          />
        )}
        {view.name === "agentDetail" && (
          <AgentDetail agentId={view.agentId} start={view.start} end={view.end} onBack={goDashboard} />
        )}
        {view.name === "manifestUpload" && (
          <ManifestUpload onOpenCall={(callId) => goCallDetail(callId, { name: "manifestUpload" })} />
        )}
        {view.name === "callDetail" && (
          <CallDetail callId={view.callId} onBack={() => setView(view.back)} />
        )}
      </div>
    </div>
  );
}
