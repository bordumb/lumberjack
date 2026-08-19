"use client";

import * as React from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * "Show the whole thing, collapsed" needs exactly one control.
 *
 * A code snippet, a coordination result and an agent's message all collapse on the same
 * five-line rule, and each had grown its own bar — same chevron, three paddings, three
 * background washes. The reader should learn this control once.
 */
function CollapseBar({
  open,
  onToggle,
  label,
  detail,
  className,
}: {
  open: boolean;
  onToggle: () => void;
  /** What the hidden thing is: a language, a diff, a summary of sections. */
  label: string;
  /** How much of it there is. Collapsing without saying how much is truncating. */
  detail?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "flex w-full items-center gap-1.5 border-b border-border px-2.5 py-1 text-left font-mono text-2xs text-muted-foreground transition-colors hover:text-foreground",
        className,
      )}
    >
      <ChevronRight className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")} />
      <span className="truncate">{label}</span>
      {detail && <span className="shrink-0 opacity-60">· {detail}</span>}
      <span className="ml-auto shrink-0 opacity-60">{open ? "collapse" : "expand"}</span>
    </button>
  );
}

/** The footer of a collapsed body: the rest of it, one click away. */
function MoreLines({ hidden, onOpen }: { hidden: number; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="w-full border-t border-border px-2.5 py-1 text-left font-mono text-2xs text-muted-foreground transition-colors hover:text-foreground"
    >
      … {hidden} more lines
    </button>
  );
}

/** The fade over a clipped body. Cut text should look cut, not accidentally ended. */
function FadeOut() {
  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-background to-transparent" />
  );
}

export { CollapseBar, FadeOut, MoreLines };
