import { useEffect, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";

export function getThemeChoice(): ThemeChoice {
  const t = document.documentElement.dataset.theme;
  return t === "light" || t === "dark" ? t : "system";
}

export function setThemeChoice(choice: ThemeChoice) {
  if (choice === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
  try {
    if (choice === "system") localStorage.removeItem("fl-theme");
    else localStorage.setItem("fl-theme", choice);
  } catch {
    /* storage unavailable */
  }
  window.dispatchEvent(new Event("fl-themechange"));
}

/** Read a CSS custom property, e.g. cssVar("--series-1"). */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Re-render when the effective theme changes (toggle or OS setting). */
export function useThemeVersion(): number {
  const [v, setV] = useState(0);
  useEffect(() => {
    const bump = () => setV((x) => x + 1);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", bump);
    window.addEventListener("fl-themechange", bump);
    return () => {
      mq.removeEventListener("change", bump);
      window.removeEventListener("fl-themechange", bump);
    };
  }, []);
  return v;
}
