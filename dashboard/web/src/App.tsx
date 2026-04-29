import { NavLink, Route, Routes } from "react-router-dom";
import ApiKeyGate from "./auth/ApiKeyGate";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Traders from "./pages/Traders";
import Decisions from "./pages/Decisions";
import Controls from "./pages/Controls";
import Config from "./pages/Config";
import Replay from "./pages/Replay";
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
            <NavLink to="/replay">Replay</NavLink>
            <NavLink to="/controls">Controls</NavLink>
            <NavLink to="/config">Config</NavLink>
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
            <Route path="/replay" element={<Replay />} />
            <Route path="/controls" element={<Controls />} />
            <Route path="/config" element={<Config />} />
          </Routes>
        </main>
      </div>
    </ApiKeyGate>
  );
}
