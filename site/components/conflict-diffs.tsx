"use client";

import { useEffect, useState } from "react";
import { MultiFileDiff, UnresolvedFile } from "@pierre/diffs/react";
import { ChevronRight, GitCompare } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { TokenHoverCard, useTokenHover } from "@/components/token-hover";
import type { SymbolInfo } from "@/components/token-hover";
import { cn } from "@/lib/utils";

type Side = { id: string; agent: string; title: string; branch: string } | null;

type ConflictFile = {
  path: string;
  symbols: Record<string, SymbolInfo>;
  base: string;
  ours: string;
  theirs: string;
  merged: string;
  conflicted: boolean;
  oursChanged: boolean;
  theirsChanged: boolean;
};

type Payload = {
  conflict: {
    id: string;
    source: string;
    severity: string;
    evidence: string;
    between: [string, string];
  };
  base: string | null;
  left: Side;
  right: Side;
  files: ConflictFile[];
};

const BASE_OPTIONS = { diffStyle: "split", theme: "pierre-dark" } as const;
const BASE_CONFLICT_OPTIONS = {
  theme: "pierre-dark",
  mergeConflictActionsType: "default",
} as const;

export function ConflictDiffs({ conflict, stand }: { conflict: string; stand: string }) {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/conflicts/${conflict}?stand=${stand}`)
      .then(async (response) => {
        if (!response.ok) throw new Error(await response.text());
        return response.json() as Promise<Payload>;
      })
      .then(setData)
      .catch((cause: Error) => setError(cause.message));
  }, [conflict, stand]);

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (!data) return <p className="text-sm text-muted-foreground">Reading both sides…</p>;

  if (data.files.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        This conflict is scope-level: it was raised from declared intent or a dependency
        edge, before either side had written a conflicting line. There is nothing to diff yet.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {data.files.map((file, index) => (
        <FileConflict
          key={file.path}
          file={file}
          left={data.left}
          right={data.right}
          defaultOpen={index === 0}
        />
      ))}
    </div>
  );
}

function FileConflict({
  file,
  left,
  right,
  defaultOpen,
}: {
  file: ConflictFile;
  left: Side;
  right: Side;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const { hovered, onTokenEnter, onTokenLeave } = useTokenHover(file.symbols);
  const diffOptions = { ...BASE_OPTIONS, onTokenEnter, onTokenLeave };
  const conflictOptions = { ...BASE_CONFLICT_OPTIONS, onTokenEnter, onTokenLeave };
  const sides = [
    { side: left, contents: file.ours, changed: file.oursChanged, label: "ours" },
    { side: right, contents: file.theirs, changed: file.theirsChanged, label: "theirs" },
  ];

  return (
    <div className="overflow-hidden rounded-lg border border-border/60 bg-card/30">
      <TokenHoverCard hovered={hovered} />
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-card/60"
      >
        <ChevronRight className={cn("h-3.5 w-3.5 shrink-0 transition-transform", open && "rotate-90")} />
        <GitCompare className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="truncate font-mono text-[12.5px]">{file.path}</span>
        <span className="ml-auto flex shrink-0 gap-1.5">
          {file.conflicted && (
            <Badge variant="outline" className="h-5 border-destructive/40 text-[10px] text-destructive">
              conflicted
            </Badge>
          )}
          {file.oursChanged && (
            <Badge variant="outline" className="h-5 text-[10px]">
              {left?.title ?? "ours"}
            </Badge>
          )}
          {file.theirsChanged && (
            <Badge variant="outline" className="h-5 text-[10px]">
              {right?.title ?? "theirs"}
            </Badge>
          )}
        </span>
      </button>

      {open && file.conflicted && (
        <div className="border-t border-border/60 p-3">
          <p className="mb-2 text-[11.5px] text-muted-foreground">
            Three-way merge of both sides. Each region below is a real conflict git could
            not resolve; the controls take one side or the other.
          </p>
          <div className="overflow-x-auto rounded-md border border-border/50">
            <UnresolvedFile
              file={{ name: file.path, contents: file.merged }}
              options={conflictOptions}
            />
          </div>
        </div>
      )}

      {open && !file.conflicted && (
        <div className="space-y-4 border-t border-border/60 p-3">
          <p className="text-[11.5px] text-muted-foreground">
            These merge cleanly as text. The conflict was raised from intent or a
            dependency edge, so the danger is semantic -- compare the two sides.
          </p>
          {sides.map(({ side, contents, changed, label }) => (
            <section key={label}>
              <header className="mb-1.5 flex items-center gap-2">
                <span className="font-mono text-[11px] text-muted-foreground">
                  base → {side?.title ?? label}
                </span>
                <span className="font-mono text-[10.5px] text-muted-foreground/60">
                  {side?.agent}
                </span>
                {!changed && (
                  <span className="font-mono text-[10.5px] text-muted-foreground/60">
                    · unchanged here
                  </span>
                )}
              </header>
              <div className="overflow-x-auto rounded-md border border-border/50">
                <MultiFileDiff
                  oldFile={{ name: file.path, contents: file.base }}
                  newFile={{ name: file.path, contents }}
                  options={diffOptions}
                />
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
