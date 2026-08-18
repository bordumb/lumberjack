"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

const COLLAPSE_OVER = 5;

/** Long output collapses by default: a session transcript is mostly file contents,
 *  and rendering all of it turns a log into a haystack. */
export function CodeBlock({ code, language }: { code: string; language?: string }) {
  const lines = code.replace(/\s+$/, "").split("\n");
  const long = lines.length > COLLAPSE_OVER;
  const [open, setOpen] = useState(!long);
  const shown = open ? lines : lines.slice(0, COLLAPSE_OVER);

  return (
    <div className="mt-2 overflow-hidden rounded-md border border-border/60 bg-muted/40">
      {long && (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex w-full items-center gap-1.5 border-b border-border/60 px-2.5 py-1.5 text-left text-[11px] text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
        >
          <ChevronRight className={cn("h-3 w-3 transition-transform", open && "rotate-90")} />
          <span className="font-mono">{language ?? "text"}</span>
          <span className="opacity-60">· {lines.length} lines</span>
          <span className="ml-auto opacity-60">{open ? "collapse" : "expand"}</span>
        </button>
      )}
      <pre className="overflow-x-auto px-3 py-2 font-mono text-[11.5px] leading-[1.55] text-foreground/85">
        <code>{shown.join("\n")}</code>
      </pre>
      {long && !open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="w-full border-t border-border/60 px-3 py-1 text-left font-mono text-[11px] text-muted-foreground hover:text-foreground"
        >
          … {lines.length - COLLAPSE_OVER} more lines
        </button>
      )}
    </div>
  );
}
