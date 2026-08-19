"use client";

import { useState } from "react";
import { File, MultiFileDiff } from "@pierre/diffs/react";
import { ChevronRight } from "lucide-react";
import { GutterAdd } from "@/components/gutter-add";
import { cn } from "@/lib/utils";
import { TokenHoverCard, useTokenHover } from "@/components/token-hover";
import type { SymbolInfo } from "@/components/token-hover";
import type { Anno } from "@/components/comments";
import { useTheme } from "@/components/theme-provider";
import { CODE_THEME } from "@/lib/theme";

const COLLAPSE_OVER = 5;
const COLLAPSED_HEIGHT = "9rem";

const EXTENSION: Record<string, string> = {
  python: "py", typescript: "ts", tsx: "tsx", javascript: "js", jsx: "jsx",
  json: "json", markdown: "md", toml: "toml", yaml: "yml", bash: "sh",
  css: "css", html: "html", sql: "sql", rust: "rs", go: "go", text: "txt",
};

/** `@pierre/diffs` picks its grammar from the file name, so give it a plausible one. */
function nameFor(target: string, language: string): string {
  if (target && /\.[A-Za-z0-9]+$/.test(target)) return target;
  return `snippet.${EXTENSION[language] ?? "txt"}`;
}

export function CodeSnippet({
  code,
  before,
  language = "text",
  target = "",
  symbols,
  annotations,
  renderAnnotation,
  onSelectLines,
  forceOpen,
}: {
  code: string;
  before?: string | null;
  language?: string;
  target?: string;
  symbols?: Record<string, SymbolInfo>;
  annotations?: { side: "additions"; lineNumber: number; metadata: Anno }[];
  renderAnnotation?: (annotation: { metadata: Anno }) => React.ReactNode;
  onSelectLines?: (range: { start: number; end: number }) => void;
  /** Held open while a composer is anchored inside it. */
  forceOpen?: boolean;
}) {
  const contents = code.replace(/\s+$/, "");
  const lines = contents.split("\n").length + (before ? before.split("\n").length : 0);
  const long = lines > COLLAPSE_OVER;
  const [open, setOpen] = useState(!long);
  // Commenting on a line you cannot see is not a thing anyone wants to do, so asking
  // to comment opens the snippet as part of the same gesture.
  const shown = open || Boolean(forceOpen);
  const pick = (range: { start: number; end: number }) => {
    setOpen(true);
    onSelectLines?.(range);
  };
  const { hovered, onTokenEnter, onTokenLeave } = useTokenHover(symbols ?? {});
  const { theme } = useTheme();

  const name = nameFor(target, language);
  const options = {
    theme: CODE_THEME,
    themeType: theme,
    diffStyle: "split",
    onTokenEnter,
    onTokenLeave,
    // Commenting is only offered where a comment can mean something: a snippet with a
    // real path behind it. Commenting on a shell command has nowhere to land.
    ...(onSelectLines
      ? {
          enableGutterUtility: true,
          enableLineSelection: true,
          onLineSelectionEnd(range: { start: number; end: number } | null) {
            if (range) pick(range);
          },
        }
      : {}),
  } as const;

  // The surrounding row already names the file and the tool. A second header inside
  // the snippet repeats it and costs a line of vertical space every time.
  const noHeader = () => null;
  // The same affordance as the conflict pages: a comment starts where the cursor is.
  const gutter = onSelectLines
    ? (getHoveredLine: () => { lineNumber: number } | undefined) => (
        <GutterAdd
          getHoveredLine={getHoveredLine}
          onPick={(line) => pick({ start: line, end: line })}
        />
      )
    : undefined;

  return (
    <div className="mt-2 overflow-hidden rounded-md border border-border/60">
      <TokenHoverCard hovered={hovered} />
      {long && (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex w-full items-center gap-1.5 border-b border-border/60 bg-muted/30 px-2.5 py-1.5 text-left text-[11px] text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
        >
          <ChevronRight className={cn("h-3 w-3 transition-transform", shown && "rotate-90")} />
          <span className="font-mono">{before ? "diff" : language}</span>
          <span className="opacity-60">· {lines} lines</span>
          <span className="ml-auto opacity-60">{shown ? "collapse" : "expand"}</span>
        </button>
      )}

      <div
        className={cn("relative overflow-x-auto", !shown && "overflow-y-hidden")}
        style={shown ? undefined : { maxHeight: COLLAPSED_HEIGHT }}
      >
        {before ? (
          <MultiFileDiff
            oldFile={{ name, contents: before }}
            newFile={{ name, contents }}
            options={options}
            lineAnnotations={annotations}
            renderAnnotation={renderAnnotation}
            renderCustomHeader={noHeader}
            renderGutterUtility={gutter}
          />
        ) : (
          <File
            file={{ name, contents }}
            options={options}
            lineAnnotations={annotations}
            renderAnnotation={renderAnnotation}
            renderCustomHeader={noHeader}
            renderGutterUtility={gutter}
          />
        )}
        {!shown && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-background to-transparent" />
        )}
      </div>

      {long && !shown && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="w-full border-t border-border/60 bg-muted/20 px-3 py-1 text-left font-mono text-[11px] text-muted-foreground transition-colors hover:text-foreground"
        >
          … {lines - COLLAPSE_OVER} more lines
        </button>
      )}
    </div>
  );
}
