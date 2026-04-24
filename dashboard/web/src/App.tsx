import { NavLink, Route, Routes } from "react-router-dom";
import ApiKeyGate from "./auth/ApiKeyGate";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Traders from "./pages/Traders";
import Decisions from "./pages/Decisions";
import Controls from "./pages/Controls";
import { clearApiKey } from "./api/client";

export default function App() {
  return (
    <ApiKeyGate>
      <div className="layout">
        <aside className="sidebar">
          <h1>POLYMARKET BOT</h1>
          <nav>
            <NavLink to="/" end>Overview</NavLink>
            <NavLink to="/positions">Positions</NavLink>
            <NavLink to="/traders">Traders</NavLink>
            <NavLink to="/decisions">Decisions</NavLink>
            <NavLink to="/controls">Controls</NavLink>
          </nav>
          <div style={{ position: "absolute", bottom: 16, left: 16 }}>
            <button
              onClick={() => {
                clearApiKey();
                window.location.reload();
              }}
              style={{ fontSize: 11 }}
            >
              Sign out
            </button>
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/positions" element={<Positions />} />
            <Route path="/traders" element={<Traders />} />
            <Route path="/decisions" element={<Decisions />} />
            <Route path="/controls" element={<Controls />} />
          </Routes>
        </main>
      </div>
    </ApiKeyGate>
  );
}
