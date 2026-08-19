"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { Prose } from "@/components/prose";
import { cn } from "@/lib/utils";

const COLLAPSE_OVER = 5;
const COLLAPSED_HEIGHT = "7.5rem";

/**
 * What an agent says, rendered rather than dumped.
 *
 * Agents report in markdown -- status tables, headed sections, fenced snippets -- and
 * as plain text that arrives as a wall of pipes and hashes. Long messages collapse on
 * the same rule as code, because a transcript where every message is a page is a
 * transcript nobody scrolls.
 */
export function AgentMessage({ text }: { text: string }) {
  const body = text.replace(/\s+$/, "");
  const lines = body.split("\n").length;
  const long = lines > COLLAPSE_OVER;
  const [open, setOpen] = useState(!long);

  if (!long) return <Prose>{body}</Prose>;

  return (
    <div className="overflow-hidden rounded-md border border-border/50">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 border-b border-border/50 bg-muted/20 px-2.5 py-1.5 text-left text-[11px] text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
      >
        <ChevronRight className={cn("h-3 w-3 transition-transform", open && "rotate-90")} />
        <span className="font-mono">markdown</span>
        <span className="opacity-60">· {lines} lines</span>
        <span className="ml-auto opacity-60">{open ? "collapse" : "expand"}</span>
      </button>

      <div
        className="relative overflow-hidden px-3 py-2"
        style={open ? undefined : { maxHeight: COLLAPSED_HEIGHT }}
      >
        <Prose>{body}</Prose>
        {!open && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-background to-transparent" />
        )}
      </div>

      {!open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="w-full border-t border-border/50 bg-muted/10 px-3 py-1 text-left font-mono text-[11px] text-muted-foreground transition-colors hover:text-foreground"
        >
          … {lines - COLLAPSE_OVER} more lines
        </button>
      )}
    </div>
  );
}
