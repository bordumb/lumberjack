"use client";

import { useEffect, useMemo, useState } from "react";
import { FileTree, useFileTree } from "@pierre/trees/react";
import { File } from "@pierre/diffs/react";
import { FileCode2, GitBranch } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { TokenHoverCard, useTokenHover } from "@/components/token-hover";
import type { SymbolInfo } from "@/components/token-hover";

type Info = { name: string; root: string; branch: string; head: string; fileCount: number };
type Tree = {
  paths: string[];
  gitStatus: { path: string; status: string }[];
  truncated?: boolean;
  unreadable?: string[];
  error?: string;
};

export function RepoBrowser({ repo }: { repo: string | null }) {
  const query = repo ? `?repo=${encodeURIComponent(repo)}` : "";
  const [info, setInfo] = useState<Info | null>(null);
  const [tree, setTree] = useState<Tree | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [contents, setContents] = useState<string | null>(null);
  const [symbols, setSymbols] = useState<Record<string, SymbolInfo>>({});
  const { hovered, onTokenEnter, onTokenLeave } = useTokenHover(symbols);

  useEffect(() => {
    void fetch(`/api/repo${query}`)
      .then((response) => response.json() as Promise<Info>)
      .then(setInfo);
    void fetch(`/api/repo/tree${query}`)
      .then((response) => response.json() as Promise<Tree>)
      .then(setTree);
    void fetch(`/api/symbols${query ? `${query}&` : "?"}`)
      .then((response) => response.json() as Promise<Record<string, SymbolInfo>>)
      .then(setSymbols)
      .catch(() => setSymbols({}));
  }, [query]);

  useEffect(() => {
    if (!selected) return;
    setContents(null);
    void fetch(
      `/api/repo/file?path=${encodeURIComponent(selected)}${repo ? `&repo=${encodeURIComponent(repo)}` : ""}`,
    )
      .then((response) => response.json() as Promise<{ contents?: string }>)
      .then((data) => setContents(data.contents ?? "(unreadable)"));
  }, [selected, repo]);

  const paths = useMemo(() => tree?.paths ?? [], [tree]);
  const { model } = useFileTree({
    paths,
    initialExpansion: "closed",
    search: true,
    onSelectionChange(selection) {
      const first = selection[0];
      // Directories come back with a trailing slash; only files open in the viewer.
      if (first && !first.endsWith("/")) setSelected(first);
    },
  });

  useEffect(() => {
    if (paths.length > 0) model.resetPaths(paths);
  }, [model, paths]);

  useEffect(() => {
    if (tree?.gitStatus) {
      model.setGitStatus(
        tree.gitStatus.map((entry) => ({ path: entry.path, status: entry.status })) as never,
      );
    }
  }, [model, tree]);

  return (
    <div className="flex h-full flex-col">
      <TokenHoverCard hovered={hovered} />
      <header className="flex flex-wrap items-center gap-3 border-b border-border/60 px-6 py-4">
        <h1 className="font-sans text-xl font-semibold tracking-[-0.02em]">
          {info?.name ?? "…"}
        </h1>
        {info && (
          <>
            <Badge variant="outline" className="h-5 gap-1 font-mono text-[10px]">
              <GitBranch className="h-3 w-3" />
              {info.branch}
            </Badge>
            <span className="font-mono text-[11px] text-muted-foreground">
              {info.head} · {info.fileCount} tracked
              {tree ? ` · ${tree.paths.length} on disk` : ""}
              {tree?.truncated ? " (truncated)" : ""}
            </span>
            <span className="ml-auto truncate font-mono text-[11px] text-muted-foreground/60">
              {info.root}
            </span>
          </>
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="flex w-72 shrink-0 flex-col border-r border-border/60">
          {tree?.error && (
            <p className="border-b border-destructive/30 bg-destructive/10 px-3 py-2 text-[11.5px] text-destructive">
              {tree.error}
            </p>
          )}
          <FileTree model={model} style={{ height: "100%" }} />
          {tree && tree.unreadable && tree.unreadable.length > 0 && (
            <p className="border-t border-border/60 px-3 py-1.5 font-mono text-[10.5px] text-muted-foreground/60">
              {tree.unreadable.length} unreadable folder(s) skipped
            </p>
          )}
        </div>

        <div className="min-w-0 flex-1 overflow-auto">
          {!selected && (
            <div className="flex h-full items-center justify-center">
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <FileCode2 className="h-4 w-4" />
                Select a file to read it.
              </p>
            </div>
          )}
          {selected && (
            <div>
              <div className="sticky top-0 z-10 border-b border-border/60 bg-background/95 px-4 py-2 font-mono text-[11.5px] text-muted-foreground backdrop-blur">
                {selected}
              </div>
              {contents === null ? (
                <p className="p-4 text-sm text-muted-foreground">Reading…</p>
              ) : (
                <File
                  file={{ name: selected, contents }}
                  options={{ theme: "pierre-dark", onTokenEnter, onTokenLeave }}
                />
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
