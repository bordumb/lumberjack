"use client";

import { useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronRight, Code2 } from "lucide-react";
import { CodeSnippet } from "@/components/code-snippet";
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
      <article
        className={cn(
          "max-w-none text-[13.5px] leading-relaxed text-foreground/90",
          "[&_h1]:mt-5 [&_h1]:mb-2 [&_h1]:font-sans [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:tracking-[-0.02em]",
          "[&_h2]:mt-5 [&_h2]:mb-2 [&_h2]:font-sans [&_h2]:text-[15px] [&_h2]:font-semibold [&_h2]:tracking-[-0.01em]",
          "[&_h3]:mt-4 [&_h3]:mb-1.5 [&_h3]:font-sans [&_h3]:text-[13.5px] [&_h3]:font-semibold",
          "[&_p]:my-2.5",
          "[&_ul]:my-2.5 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2.5 [&_ol]:list-decimal [&_ol]:pl-5",
          "[&_li]:my-1",
          "[&_a]:text-primary [&_a]:underline [&_a]:underline-offset-2",
          "[&_strong]:font-semibold [&_strong]:text-foreground",
          "[&_code]:rounded [&_code]:bg-muted/60 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[12px]",
          "[&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border/60 [&_pre]:bg-muted/40 [&_pre]:p-3",
          "[&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-[11.5px]",
          "[&_blockquote]:my-3 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground",
          "[&_table]:my-3 [&_table]:w-full [&_table]:border-collapse [&_table]:text-[12.5px]",
          "[&_th]:border [&_th]:border-border/60 [&_th]:bg-muted/40 [&_th]:px-2.5 [&_th]:py-1.5 [&_th]:text-left [&_th]:font-medium",
          "[&_td]:border [&_td]:border-border/60 [&_td]:px-2.5 [&_td]:py-1.5 [&_td]:align-top",
          "[&_hr]:my-5 [&_hr]:border-border/60",
        )}
      >
        <Markdown remarkPlugins={[remarkGfm]}>{body}</Markdown>
      </article>

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
