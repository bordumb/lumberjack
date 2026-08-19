"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { MoreHorizontal, Pause, Pencil, Play, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Lifecycle } from "@/lib/types";

type Props = {
  stand: string;
  repo: string | null;
  lifecycle: Lifecycle;
  title: string;
  onChanged: () => void;
};

async function control(stand: string, body: Record<string, unknown>) {
  const response = await fetch(`/api/stands/${stand}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = (await response.json()) as { error?: string };
  if (!response.ok) throw new Error(data.error ?? "failed");
  return data;
}

export function StandControls({ stand, repo, lifecycle, title, onChanged }: Props) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [name, setName] = useState(title);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => setName(title), [title]);

  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setMenu(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const act = async (body: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await control(stand, { repo, ...body });
      onChanged();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Only a run that is actually running can be paused. A stale one has nothing to stop.
  const running = lifecycle === "live";
  const resumable = lifecycle === "halted" || lifecycle === "stale";

  return (
    <div className="relative flex items-center gap-2" ref={box}>
      {error && (
        <span className="max-w-md truncate font-mono text-[11px] text-destructive" title={error}>
          {error}
        </span>
      )}

      {running && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void act({ action: "pause" })}
          className="inline-flex items-center gap-1.5 rounded-md bg-destructive px-2.5 py-1.5 text-[12px] font-medium text-white disabled:opacity-40"
        >
          <Pause className="h-3.5 w-3.5" />
          Pause
        </button>
      )}

      {resumable && (
        <button
          type="button"
          disabled={busy}
          title="starts a new run from this one's branches"
          onClick={() => void act({ action: "continue" })}
          className="inline-flex items-center gap-1.5 rounded-md bg-sky-600 px-2.5 py-1.5 text-[12px] font-medium text-white disabled:opacity-40"
        >
          <Play className="h-3.5 w-3.5" />
          Continue
        </button>
      )}

      <button
        type="button"
        aria-label="run options"
        onClick={() => setMenu((value) => !value)}
        className={cn(
          "rounded-md border border-border p-1.5 text-muted-foreground transition-colors hover:text-foreground",
          menu && "bg-muted/60 text-foreground",
        )}
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>

      {menu && !renaming && !confirming && (
        <div className="absolute right-0 top-9 z-40 w-44 overflow-hidden rounded-lg border border-border bg-popover shadow-xl">
          <button
            type="button"
            onClick={() => {
              setRenaming(true);
              setMenu(false);
            }}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] hover:bg-muted/60"
          >
            <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
            Rename
          </button>
          <button
            type="button"
            onClick={() => {
              setConfirming(true);
              setMenu(false);
            }}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] text-destructive hover:bg-destructive/10"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
        </div>
      )}

      {renaming && (
        <div className="absolute right-0 top-9 z-40 w-80 rounded-lg border border-border bg-popover p-3 shadow-xl">
          <input
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setRenaming(false);
              if (event.key === "Enter" && name.trim()) {
                void act({ action: "rename", name }).then(() => setRenaming(false));
              }
            }}
            className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-[13px] outline-none focus:border-primary/60"
          />
          <p className="mt-1.5 font-mono text-[10.5px] text-muted-foreground/60">
            adds a label; the original goal stays in the log
          </p>
        </div>
      )}

      {confirming && (
        <div className="absolute right-0 top-9 z-40 w-96 rounded-lg border border-destructive/40 bg-popover p-3 shadow-xl">
          <p className="text-[13px]">Delete this run?</p>
          <p className="mt-1 text-[12px] text-muted-foreground">
            Removes its ledger and worktrees. Branches are kept, so any work that never
            landed survives.
          </p>
          <div className="mt-2.5 flex items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void act({ action: "delete", force: true }).then(() => {
                  setConfirming(false);
                  router.push(repo ? `/?repo=${encodeURIComponent(repo)}` : "/");
                })
              }
              className="rounded-md bg-destructive px-2.5 py-1 text-[12px] font-medium text-white disabled:opacity-40"
            >
              Delete
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-md border border-border px-2.5 py-1 text-[12px] text-muted-foreground hover:text-foreground"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
