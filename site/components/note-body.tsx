"use client";

import { useState } from "react";
import { ChevronRight, Code2 } from "lucide-react";
import { CodeSnippet } from "@/components/code-snippet";
import { Prose } from "@/components/prose";
import { cn } from "@/lib/utils";

/**
 * Notes are written by agents, and agents write markdown -- tables, headings, fenced
 * code. Rendering it is the difference between a note being read and being skipped;
 * the source stays one click away for anything the renderer flattens.
 */
export function NoteBody({ body }: { body: string }) {
  const [source, setSource] = useState(false);

  return (
    <div>
      <Prose>{body}</Prose>

      <button
        type="button"
        onClick={() => setSource((value) => !value)}
        className="mt-4 flex items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronRight className={cn("h-3 w-3 transition-transform", source && "rotate-90")} />
        <Code2 className="h-3 w-3" />
        source
      </button>
      {source && <CodeSnippet code={body} language="markdown" target="note.md" />}
    </div>
  );
}
