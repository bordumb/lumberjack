"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { DEFAULT_THEME, THEME_KEY } from "@/lib/theme";
import type { Theme } from "@/lib/theme";

type ThemeHandle = { theme: Theme; setTheme: (theme: Theme) => void };

const ThemeContext = createContext<ThemeHandle | null>(null);

function apply(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  // Scrollbars, form controls and the canvas behind an over-scroll are the browser's to
  // paint, and only `color-scheme` tells it which way to paint them.
  root.style.colorScheme = theme;
}

/**
 * Reads the theme already on the document.
 *
 * The bootstrap script in the head has run by the time React hydrates, so the DOM is
 * the source of truth for the first client render, and no second pass is needed to
 * catch up with what was stored.
 */
function current(): Theme {
  if (typeof document === "undefined") return DEFAULT_THEME;
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, remember] = useState<Theme>(current);

  useEffect(() => {
    apply(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // Private windows and blocked storage still get a working toggle, just not one
      // that survives a reload.
    }
    remember(next);
  }, []);

  const handle = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={handle}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeHandle {
  const handle = useContext(ThemeContext);
  if (!handle) throw new Error("useTheme must be used inside <ThemeProvider>");
  return handle;
}
