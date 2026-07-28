import { useState } from "react";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import ParameterAgents from "./pages/ParameterAgents";
import AgentDetail from "./pages/AgentDetail";
import ManifestUpload from "./pages/ManifestUpload";
import CallDetail from "./pages/CallDetail";
import CategoryAgents from "./pages/CategoryAgents";
import AgentCalls from "./pages/AgentCalls";

export default function App() {
  const [session, setSession] = useState(null);
  // view.name:
  //   'dashboard' | 'parameterAgents' | 'agentDetail' | 'manifestUpload' | 'callDetail'
  //   | 'categoryAgents' (feature 2.1) | 'agentCalls' (feature 2.2)
  const [view, setView] = useState({ name: "dashboard" });

  if (!session) {
    return <Login onLogin={setSession} />;
  }

  function goDashboard() { setView({ name: "dashboard" }); }
  function goParameterAgents(parameter, start, end) { setView({ name: "parameterAgents", parameter, start, end }); }
  function goAgentDetail(agentId, start, end) { setView({ name: "agentDetail", agentId, start, end }); }
  function goManifestUpload() { setView({ name: "manifestUpload" }); }
  function goCallDetail(callId, back) { setView({ name: "callDetail", callId, back: back || { name: "dashboard" } }); }

  // Feature 2: dashboard stat card -> agents -> that agent's calls -> call detail (audio + evidence)
  function goCategoryAgents(category, label, start, end) {
    setView({ name: "categoryAgents", category, label, start, end });
  }
  function goAgentCalls(category, label, agentId, agentName, start, end) {
    setView({ name: "agentCalls", category, label, agentId, agentName, start, end });
  }

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
        {view.name === "dashboard" && (
          <Dashboard
            onSelectParameter={goParameterAgents}
            onSelectCategory={goCategoryAgents}
            onSelectAgent={goAgentDetail}
          />
        )}

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

        {/* feature 2.1: agents ranked within the clicked stat-card category */}
        {view.name === "categoryAgents" && (
          <CategoryAgents
            category={view.category}
            label={view.label}
            start={view.start}
            end={view.end}
            onBack={goDashboard}
            onSelectAgent={(agentId, agentName) =>
              goAgentCalls(view.category, view.label, agentId, agentName, view.start, view.end)
            }
          />
        )}

        {/* feature 2.2: that agent's calls within the category */}
        {view.name === "agentCalls" && (
          <AgentCalls
            category={view.category}
            label={view.label}
            agentId={view.agentId}
            agentName={view.agentName}
            start={view.start}
            end={view.end}
            onBack={() => goCategoryAgents(view.category, view.label, view.start, view.end)}
            onSelectCall={(callId) => goCallDetail(callId, view)}
          />
        )}

        {view.name === "manifestUpload" && (
          <ManifestUpload onOpenCall={(callId) => goCallDetail(callId, { name: "manifestUpload" })} />
        )}

        {/* feature 2.3: parameter marks + audio evidence for one call */}
        {view.name === "callDetail" && (
          <CallDetail callId={view.callId} onBack={() => setView(view.back)} />
        )}
      </div>
    </div>
  );
}
