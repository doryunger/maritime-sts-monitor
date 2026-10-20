import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import Runs from "./pages/Runs";
import Run from "./pages/Run";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <header className="top">
        <Link to="/" className="brand">Maritime STS Monitor</Link>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Runs />} />
          <Route path="/runs/:no/*" element={<Run />} />
        </Routes>
      </main>
    </BrowserRouter>
  </React.StrictMode>,
);
