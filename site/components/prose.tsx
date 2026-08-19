"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

/**
 * Markdown as agents actually write it: headings, tables, fenced code, task lists.
 *
 * The styling lives in one place because the same prose appears in a blackboard note
 * and in a transcript, and prose that reads differently depending on where it landed
 * is prose the reader has to re-learn.
 */
export function Prose({ children, className }: { children: string; className?: string }) {
  return (
    <article
      className={cn(
        "max-w-none text-[13.5px] leading-relaxed text-foreground/90",
        "[&>*:first-child]:mt-0 [&>*:last-child]:mb-0",
        "[&_h1]:mt-5 [&_h1]:mb-2 [&_h1]:font-sans [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:tracking-[-0.02em]",
        "[&_h2]:mt-5 [&_h2]:mb-2 [&_h2]:font-sans [&_h2]:text-[15px] [&_h2]:font-semibold [&_h2]:tracking-[-0.01em]",
        "[&_h3]:mt-4 [&_h3]:mb-1.5 [&_h3]:font-sans [&_h3]:text-[13.5px] [&_h3]:font-semibold",
        "[&_p]:my-2.5",
        "[&_ul]:my-2.5 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2.5 [&_ol]:list-decimal [&_ol]:pl-5",
        "[&_li]:my-1 [&_li>p]:my-0",
        "[&_a]:text-primary [&_a]:underline [&_a]:underline-offset-2",
        "[&_strong]:font-semibold [&_strong]:text-foreground",
        "[&_code]:rounded [&_code]:bg-muted/60 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[12px]",
        "[&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border/60 [&_pre]:bg-muted/40 [&_pre]:p-3",
        "[&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-[11.5px]",
        "[&_blockquote]:my-3 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground",
        // Tables are why this exists: agents report status in them constantly.
        "[&_table]:my-3 [&_table]:w-full [&_table]:border-collapse [&_table]:text-[12.5px]",
        "[&_th]:border [&_th]:border-border/60 [&_th]:bg-muted/40 [&_th]:px-2.5 [&_th]:py-1.5 [&_th]:text-left [&_th]:font-medium",
        "[&_td]:border [&_td]:border-border/60 [&_td]:px-2.5 [&_td]:py-1.5 [&_td]:align-top",
        "[&_hr]:my-5 [&_hr]:border-border/60",
        className,
      )}
    >
      <Markdown remarkPlugins={[remarkGfm]}>{children}</Markdown>
    </article>
  );
}
