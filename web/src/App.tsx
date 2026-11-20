import { useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { useReadOnly } from "./components/server";
import { getThemeChoice, setThemeChoice, type ThemeChoice } from "./components/theme";
import { AircraftList, AircraftPage } from "./pages/Aircraft";
import { Compare } from "./pages/Compare";
import { Library } from "./pages/Library";
import { FlightPage } from "./pages/FlightPage";
import { Overview } from "./pages/Overview";
import { SignalsView } from "./pages/SignalsView";
import { SpectrumView } from "./pages/SpectrumView";
import { TuningView } from "./pages/TuningView";
import { EventsView } from "./pages/EventsView";
import { QualityView } from "./pages/QualityView";

export function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <TopBar />
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/aircraft" element={<AircraftList />} />
        <Route path="/aircraft/:key" element={<AircraftPage />} />
        <Route path="/compare" element={<Compare />} />
        <Route path="/flights/:id" element={<FlightPage />}>
          <Route index element={<Overview />} />
          <Route path="signals" element={<SignalsView />} />
          <Route path="tuning" element={<TuningView />} />
          <Route path="spectrum" element={<SpectrumView />} />
          <Route path="events" element={<EventsView />} />
          <Route path="quality" element={<QualityView />} />
        </Route>
        <Route path="*" element={<main className="page empty">This page does not exist.</main>} />
      </Routes>
    </BrowserRouter>
  );
}

function TopBar() {
  const [theme, setTheme] = useState<ThemeChoice>(getThemeChoice);
  const readOnly = useReadOnly();
  const choose = (t: ThemeChoice) => {
    setThemeChoice(t);
    setTheme(t);
  };
  return (
    <header className="topbar">
      <NavLink to="/" className="brand" aria-label="FlightLens home">
        <svg width="20" height="20" viewBox="0 0 32 32" fill="none" aria-hidden>
          <circle cx="16" cy="16" r="12.5" stroke="currentColor" strokeWidth="3" />
          <circle cx="16" cy="16" r="4" fill="currentColor" />
        </svg>
        FlightLens
      </NavLink>
      <nav className="nav">
        <NavLink to="/" end>
          Flights
        </NavLink>
        <NavLink to="/aircraft">Aircraft</NavLink>
      </nav>
      <div className="spacer" />
      {readOnly && (
        <span className="chip" title="Uploads, edits and deletes are turned off on this server">Read-only demo</span>
      )}
      <div className="segmented" role="group" aria-label="Theme">
        {(["system", "light", "dark"] as const).map((t) => (
          <button key={t} aria-pressed={theme === t} onClick={() => choose(t)}>
            {t === "system" ? "Auto" : t === "light" ? "Light" : "Dark"}
          </button>
        ))}
      </div>
    </header>
  );
}
