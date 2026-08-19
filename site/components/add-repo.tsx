"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowUp, Check, FolderGit2, FolderOpen, X } from "lucide-react";
import { cn } from "@/lib/utils";

type DirEntry = { name: string; path: string; isRepo: boolean };
type Listing = { cwd: string; parent: string | null; entries: DirEntry[] };

/**
 * A server-side folder browser.
 *
 * The browser's own directory pickers deliberately withhold the absolute path, and git
 * needs one. The dashboard runs on the same machine as the repositories it reads, so
 * listing directories server-side is the same trust boundary a terminal already has.
 */
export function AddRepo({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [listing, setListing] = useState<Listing | null>(null);
  const [manual, setManual] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (at: string | null) => {
    const query = at ? `?at=${encodeURIComponent(at)}` : "";
    const response = await fetch(`/api/repos/browse${query}`);
    setListing((await response.json()) as Listing);
  }, []);

  useEffect(() => {
    void load(null);
  }, [load]);

  const add = async (root: string) => {
    setBusy(true);
    setError(null);
    const response = await fetch("/api/repos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: root }),
    });
    setBusy(false);
    if (!response.ok) {
      setError(((await response.json()) as { error: string }).error);
      return;
    }
    onAdded();
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-6"
      onClick={onClose}
    >
      <div
        className="flex h-[32rem] w-full max-w-2xl flex-col rounded-xl border border-border bg-card shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
          <FolderOpen className="h-4 w-4 text-primary" />
          <h2 className="font-sans text-sm font-medium tracking-[-0.01em]">Add a project</h2>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex items-center gap-2 border-b border-border/60 px-4 py-2">
          <button
            type="button"
            disabled={!listing?.parent}
            onClick={() => void load(listing?.parent ?? null)}
            className="rounded-md border border-border p-1 text-muted-foreground disabled:opacity-30 hover:text-foreground"
          >
            <ArrowUp className="h-3.5 w-3.5" />
          </button>
          <span className="truncate font-mono text-[11.5px] text-muted-foreground">
            {listing?.cwd ?? "…"}
          </span>
        </div>

        <div className="flex-1 overflow-y-auto p-2">
          {listing?.entries.length === 0 && (
            <p className="p-4 text-sm text-muted-foreground">No folders here.</p>
          )}
          {listing?.entries.map((entry) => (
            <div
              key={entry.path}
              className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted/50"
            >
              <button
                type="button"
                onClick={() => void load(entry.path)}
                className="flex flex-1 items-center gap-2 text-left"
              >
                {entry.isRepo ? (
                  <FolderGit2 className="h-3.5 w-3.5 shrink-0 text-primary" />
                ) : (
                  <FolderOpen className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                )}
                <span className={cn("truncate text-[13px]", entry.isRepo && "text-primary")}>
                  {entry.name}
                </span>
              </button>
              {entry.isRepo && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void add(entry.path)}
                  className="inline-flex items-center gap-1 rounded-md bg-primary px-2 py-0.5 text-[11px] font-medium text-primary-foreground disabled:opacity-40"
                >
                  <Check className="h-3 w-3" />
                  add
                </button>
              )}
            </div>
          ))}
        </div>

        <footer className="space-y-2 border-t border-border/60 px-4 py-3">
          {error && <p className="text-[12px] text-destructive">{error}</p>}
          <div className="flex gap-2">
            <input
              value={manual}
              onChange={(event) => setManual(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && manual.trim()) void add(manual.trim());
              }}
              placeholder="or paste a path"
              className="flex-1 rounded-md border border-border bg-background px-2.5 py-1.5 font-mono text-[12px] outline-none focus:border-primary/60"
            />
            <button
              type="button"
              disabled={!manual.trim() || busy}
              onClick={() => void add(manual.trim())}
              className="rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
            >
              add
            </button>
          </div>
          <p className="font-mono text-[10.5px] text-muted-foreground/60">
            Only folders containing a .git directory can be added.
          </p>
        </footer>
      </div>
    </div>
  );
}
