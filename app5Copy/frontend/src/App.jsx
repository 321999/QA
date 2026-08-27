import { useEffect, useRef, useState } from "react";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import ParameterAgents from "./pages/ParameterAgents";
import AgentDetail from "./pages/AgentDetail";
import ManifestUpload from "./pages/ManifestUpload";
import CallDetail from "./pages/CallDetail";
import CategoryAgents from "./pages/CategoryAgents";
import AgentCalls from "./pages/AgentCalls";
import Reports from "./pages/Reports";
import DateRangePicker from "./components/DateRangePicker";
import ChangePasswordModal from "./components/ChangePasswordModal";
import MenuButton from "./components/MenuButton";

// Single "Menu" button, top-right, opens a dropdown with everything that used
// to be separate buttons (Dashboard / Upload manifest / Sign out) plus
// Change password and the new Reports page.
function NavMenu({ username, currentView, onDashboard, onUpload, onReports, onChangePassword, onLogout }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    function onClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  function pick(action) {
    setOpen(false);
    action();
  }

  return (
    <div className="menu-wrap" ref={ref}>
      <button className="menu-btn" type="button" onClick={() => setOpen((v) => !v)}>

        <MenuButton/>
      </button>
      {open && (
        <div className="menu-dropdown">
          <div className="menu-user">Signed in as {username}</div>
          <button
            type="button"
            className={currentView === "dashboard" ? "active" : ""}
            onClick={() => pick(onDashboard)}
          >
            Dashboard
          </button>
          <button
            type="button"
            className={currentView === "reports" ? "active" : ""}
            onClick={() => pick(onReports)}
          >
            Reports
          </button>
          <button
            type="button"
            className={currentView === "manifestUpload" ? "active" : ""}
            onClick={() => pick(onUpload)}
          >
            Upload File
          </button>
          <button type="button" onClick={() => pick(onChangePassword)}>
            Change password
          </button>
          <button type="button" className="danger" onClick={() => pick(onLogout)}>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState(null);
  const [{ start, end }, setRange] = useState(defaultRange()); // foir the daterange picker globally 
  // view.name:
  //   'dashboard' | 'parameterAgents' | 'agentDetail' | 'manifestUpload' | 'callDetail'
  //   | 'categoryAgents' (feature 2.1) | 'agentCalls' (feature 2.2) | 'reports'
  const [view, setView] = useState({ name: "dashboard" });
  const [showChangePassword, setShowChangePassword] = useState(false);

  if (!session) {
    return <Login onLogin={setSession} />;
  }

  function goDashboard() { setView({ name: "dashboard" }); }
  function goParameterAgents(parameter) { setView({ name: "parameterAgents", parameter }); }
  function goAgentDetail(agentId) { setView({ name: "agentDetail", agentId}); }
  function goManifestUpload() { setView({ name: "manifestUpload" }); }
  function goReports() { setView({ name: "reports" }); }
  function goCallDetail(callId, back) { setView({ name: "callDetail", callId, back: back || { name: "dashboard" } }); }

  // Feature 2: dashboard stat card -> agents -> that agent's calls -> call detail (audio + evidence)
  function goCategoryAgents(category, label) {
    setView({ name: "categoryAgents", category, label });
  }
  function goAgentCalls(category, label, agentId, agentName, back) {
    setView({ name: "agentCalls", category, label, agentId, agentName, back: back || { name: "categoryAgents", category, label } });
  }

  function logout() {
    setSession(null);
    setView({ name: "dashboard" });
  }

  
function fmt(d) {
  return d.toISOString().slice(0, 10);
}
// setting for the datetranger picker 

function defaultRange() {
  const e = new Date();
  const s = new Date();
  s.setDate(s.getDate() - 30);
  return { start: fmt(s), end: fmt(e) };
}
  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="brand"><span className="dot" />Athena QA {view.name}</div>
        <div className="topbar-center">
    <DateRangePicker
        start={start}
        end={end}
        onChange={(s, e) => setRange({ start: s, end: e })}
    />
</div>
        <div className="topbar-right">
          <NavMenu
            username={session.username}
            currentView={view.name}
            onDashboard={goDashboard}
            onUpload={goManifestUpload}
            onReports={goReports}
            onChangePassword={() => setShowChangePassword(true)}
            onLogout={logout}
          />
        </div>
      </div>
{/* <h5>call detail </h5> */}
      <div className="content">
        {view.name === "dashboard" && (
        <Dashboard
    start={start}
    end={end}
    onSelectParameter={goParameterAgents}
    onSelectCategory={goCategoryAgents}
    onSelectAgent={goAgentDetail}
/>
        )}

        {view.name === "parameterAgents" && (
          <ParameterAgents
            parameter={view.parameter}
            start={start}
            end={end}
            onBack={goDashboard}
            onSelectAgent={goAgentDetail}
          />
        )}

        {view.name === "agentDetail" && ( 
          <AgentDetail
            agentId={view.agentId}
            start={start}
            end={end}
            onBack={goDashboard}
            onSelectCategory={(category, label, agentName) =>
              goAgentCalls(category, label, view.agentId, agentName, { name: "agentDetail", agentId: view.agentId })
            }
          />
        )}

        {/* feature 2.1: agents ranked within the clicked stat-card category */}
        {view.name === "categoryAgents" && (
          <CategoryAgents
            category={view.category}
            label={view.label}
            start={start}
            end={end}
            onBack={goDashboard}
            onSelectAgent={(agentId, agentName) =>
              goAgentCalls(view.category, view.label, agentId, agentName)
            }
          />
        )}

        {/* feature 2.2: that agent's calls within the category - reached either
            from CategoryAgents (dashboard stat card flow) or straight from
            AgentDetail's clickable Total/Average/Good/Poor cards */}
        {view.name === "agentCalls" && (
          <AgentCalls
            category={view.category}
            label={view.label}
            agentId={view.agentId}
            agentName={view.agentName}
            start={start}
            end={end}
            onBack={() => setView(view.back)}
            onSelectCall={(callId) => goCallDetail(callId, view)}
          />
        )}

        {view.name === "manifestUpload" && (
          <ManifestUpload onOpenCall={(callId) => goCallDetail(callId, { name: "manifestUpload" })} />
        )}

        {/* Agent report: agent / recording / marks per parameter, one row per call */}
        {view.name === "reports" && (
          <Reports
            start={start}
            end={end}
            onSelectCall={(callId) => goCallDetail(callId, { name: "reports" })}
          />
        )}

        {/* feature 2.3: parameter marks + audio evidence for one call */}
        {view.name === "callDetail" && (
          <CallDetail callId={view.callId} onBack={() => setView(view.back)} />
        )}
      </div>

      {showChangePassword && (
        <ChangePasswordModal
          username={session.username}
          onClose={() => setShowChangePassword(false)}
        />
      )}
    </div>
  );
}
