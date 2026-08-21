import { useState } from "react";
import { api } from "../api";
// import logo from "../assets/hero.png"; // when i will get thelink i will put it here 
// import logo from "../assets/img-01.png" ;
import logo from "../assets/logo_transparent.png" ;

import logo2 from "../assets/logo.ico" ;



const BAR_HEIGHTS = [14, 28, 40, 22, 55, 33, 18, 46, 60, 24, 38, 50, 20, 44, 30, 16, 52, 26, 42, 19];

export default function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const data = await api.login(username, password);
      onLogin(data);
    } catch (err) {
      setError(err.message || "Could not sign in");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="logo-wrapper">
              <img src={logo} alt="Logo" className="company-logo" />
            </div>
      {/* <div className="login-visual">
        <div className="eyebrow">Call Quality Intelligence</div>
        <h1>Every call, scored on what matters.</h1>
        <p>
          Track audits across your floor against the 16-point QA checklist —
          from greeting to close — and see exactly where each agent needs coaching.
        </p>
        <div className="waveform" aria-hidden="true">
          {BAR_HEIGHTS.map((h, i) => (
            <span key={i} style={{ height: `${h}px` }} />
          ))}
        </div>
      </div> */}

      <div className="login-panel">
        <form className="login-card" onSubmit={handleSubmit}>
          {/* checking for hte bavkefround imge as tehlogin  */}
          {/* <div className="login-logo">
      <img src={logo2} alt="Athena BPO" />
    </div> */}
          <div className="brand">QA  </div>
          <div className="sub">Sign in to view the audit dashboard</div>

          {error && <div className="login-error">{error}</div>}

          <div className="field">
            <label htmlFor="username">Username</label>
            <input
              id="username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>

          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          <button className="btn-primary" type="submit" disabled={loading}>
            {loading ? "Signing in..." : "Sign in"}
          </button>

          {/* <div className="login-hint">For YBL demo: admin / admin123</div> */}
        </form>
      </div>
    </div>
  );
// return (
//   <div className="login-shell">

//     <div className="login-panel">
//       <form className="login-card" onSubmit={handleSubmit}>

//         <div className="logo-wrapper">
//           <img
//             src={logo}
//             alt="Athena BPO"
//             className="company-logo"
//           />
//         </div>

//         <div className="brand">QA</div>

//         <div className="sub">
//           Sign in to view the audit dashboard
//         </div>

//         {error && <div className="login-error">{error}</div>}

//         <div className="field">
//           <label htmlFor="username">Username</label>
//           <input
//             id="username"
//             type="text"
//             autoComplete="username"
//             value={username}
//             onChange={(e) => setUsername(e.target.value)}
//             required
//           />
//         </div>

//         <div className="field">
//           <label htmlFor="password">Password</label>
//           <input
//             id="password"
//             type="password"
//             autoComplete="current-password"
//             value={password}
//             onChange={(e) => setPassword(e.target.value)}
//             required
//           />
//         </div>

//         <button
//           className="btn-primary"
//           type="submit"
//           disabled={loading}
//         >
//           {loading ? "Signing in..." : "Sign in"}
//         </button>

//         <div className="login-hint">
//           demo: admin / admin123
//         </div>

//       </form>
//     </div>

//   </div>
// );
}


// import { useState } from "react";
// import { api } from "../api";
// // import logo from "../assets/hero.png"; // when i will get thelink i will put it here 
// import logo from "../assets/logo.ico"; // when i will get thelink i will put it here 


// const BAR_HEIGHTS = [14, 28, 40, 22, 55, 33, 18, 46, 60, 24, 38, 50, 20, 44, 30, 16, 52, 26, 42, 19];

// export default function Login({ onLogin }) {
//   const [username, setUsername] = useState("");
//   const [password, setPassword] = useState("");
//   const [error, setError] = useState("");
//   const [loading, setLoading] = useState(false);

//   async function handleSubmit(e) {
//     e.preventDefault();
//     setError("");
//     setLoading(true);
//     try {
//       const data = await api.login(username, password);
//       onLogin(data);
//     } catch (err) {
//       setError(err.message || "Could not sign in");
//     } finally {
//       setLoading(false);
//     }
//   }

//   return (
//     <div className="login-shell">
//       {/* <div className="login-visual">
//         <div className="eyebrow">Call Quality Intelligence</div>
//         <h1>Every call, scored on what matters.</h1>
//         <p>
//           Track audits across your floor against the 16-point QA checklist —
//           from greeting to close — and see exactly where each agent needs coaching.
//         </p>
//         <div className="waveform" aria-hidden="true">
//           {BAR_HEIGHTS.map((h, i) => (
//             <span key={i} style={{ height: `${h}px` }} />
//           ))}
//         </div>
//       </div> */}

//       <div className="login-panel">
//         <form className="login-card" onSubmit={handleSubmit}>
//           <img src={logo} alt="Logo" className="company-logo" />
//           <div className="brand">QA</div>
//           <div className="sub">Sign in to view the audit dashboard</div>

//           {error && <div className="login-error">{error}</div>}

//           <div className="field">
//             <label htmlFor="username">Username</label>
//             <input
//               id="username"
//               type="text"
//               autoComplete="username"
//               value={username}
//               onChange={(e) => setUsername(e.target.value)}
//               required
//             />
//           </div>

//           <div className="field">
//             <label htmlFor="password">Password</label>
//             <input
//               id="password"
//               type="password"
//               autoComplete="current-password"
//               value={password}
//               onChange={(e) => setPassword(e.target.value)}
//               required
//             />
//           </div>

//           <button className="btn-primary" type="submit" disabled={loading}>
//             {loading ? "Signing in..." : "Sign in"}
//           </button>

//           <div className="login-hint">demo: admin / admin123</div>
//         </form>
//       </div>
//     </div>
//   );
// }

