"use client";

import { useEffect } from "react";
import { Check, Moon, Settings, Sun, X } from "lucide-react";
import { useTheme } from "@/components/theme-provider";
import type { Theme } from "@/lib/theme";
import { cn } from "@/lib/utils";

const APPEARANCE: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
];

/**
 * Settings for the dashboard itself.
 *
 * Nothing here touches a repository or a run: these are preferences of this browser,
 * which is why they are kept in localStorage and not in the ledger every agent reads.
 */
export function SettingsModal({ onClose }: { onClose: () => void }) {
  const { theme, setTheme } = useTheme();

  useEffect(() => {
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", dismiss);
    return () => window.removeEventListener("keydown", dismiss);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-6"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        className="w-full max-w-sm rounded-xl border border-border bg-card shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
          <Settings className="h-4 w-4 text-primary" />
          <h2 className="font-sans text-sm font-medium tracking-[-0.01em]">Settings</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="close settings"
            className="ml-auto text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="px-4 py-4">
          <p className="text-[12px] font-medium">Appearance</p>
          <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
            Applies to the dashboard and the code it renders. Kept in this browser.
          </p>
          <div role="radiogroup" aria-label="Appearance" className="mt-2.5 grid grid-cols-2 gap-2">
            {APPEARANCE.map(({ value, label, icon: Icon }) => {
              const picked = theme === value;
              return (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={picked}
                  onClick={() => setTheme(value)}
                  className={cn(
                    "flex items-center gap-2 rounded-lg border px-3 py-2 text-[12.5px] transition-colors",
                    picked
                      ? "border-primary/50 bg-primary/10 text-primary"
                      : "border-border text-muted-foreground hover:border-primary/30 hover:text-foreground",
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  {label}
                  {picked && <Check className="ml-auto h-3.5 w-3.5 shrink-0" />}
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
