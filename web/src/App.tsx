import { useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { getThemeChoice, setThemeChoice, type ThemeChoice } from "./components/theme";
import { Library } from "./pages/Library";
import { FlightPage } from "./pages/FlightPage";
import { Overview } from "./pages/Overview";
import { SignalsView } from "./pages/SignalsView";
import { EventsView } from "./pages/EventsView";
import { QualityView } from "./pages/QualityView";

export function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <TopBar />
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/flights/:id" element={<FlightPage />}>
          <Route index element={<Overview />} />
          <Route path="signals" element={<SignalsView />} />
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
      </nav>
      <div className="spacer" />
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
