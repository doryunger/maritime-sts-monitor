import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import Runs from "./pages/Runs";
import Run from "./pages/Run";
import Jobs from "./pages/Jobs";
import HotspotTests from "./pages/HotspotTests";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <header className="top">
        <Link to="/" className="brand">Maritime STS Monitor</Link>
        <nav className="toolbar"><Link to="/">Runs</Link><Link to="/jobs">Jobs</Link><Link to="/hotspot-tests">Hotspot tests</Link></nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Runs />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/hotspot-tests" element={<HotspotTests />} />
          <Route path="/hotspot-tests/runs/:no/*" element={<Run hotspotTest />} />
          <Route path="/runs/:no/*" element={<Run />} />
        </Routes>
      </main>
    </BrowserRouter>
  </React.StrictMode>,
);
