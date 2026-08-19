"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { CheckCircle2, CircleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CodeSnippet } from "@/components/code-snippet";
import { CommentCard, CommentComposer, useComments } from "@/components/comments";
import type { Target } from "@/components/comments";
import type { ReviewComment } from "@/lib/types";
import type { SymbolInfo } from "@/components/token-hover";
import { isCoordination, toolIcon } from "@/components/tool-icon";
import { cn } from "@/lib/utils";
import type { LogEntry } from "@/lib/types";
import type { TaskView } from "@/lib/weave";

function time(at: number | null): string {
  return at ? new Date(at).toLocaleTimeString([], { hour12: false }) : "";
}

export function LogStream({ workstream, stand }: { workstream: string; stand: string }) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [symbols, setSymbols] = useState<Record<string, SymbolInfo>>({});
  const [pending, setPending] = useState<Target | null>(null);
  const { comments, post, resolve } = useComments(stand);
  const [task, setTask] = useState<TaskView | null>(null);
  const [follow, setFollow] = useState(true);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const source = new EventSource(`/api/agents/${workstream}/log?stand=${stand}`);
    source.onmessage = (event) => {
      const data = JSON.parse(event.data) as { entries: LogEntry[]; task: TaskView };
      setEntries(data.entries);
      setTask(data.task);
    };
    return () => source.close();
  }, [workstream, stand]);

  useEffect(() => {
    fetch(`/api/symbols?stand=${stand}`)
      .then((response) => response.json() as Promise<Record<string, SymbolInfo>>)
      .then(setSymbols)
      .catch(() => setSymbols({}));
  }, [stand]);

  useEffect(() => {
    if (follow) bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries, follow]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-border/60 px-1 pb-3">
        <span className="font-mono text-xs text-muted-foreground">
          {entries.length} entries
        </span>
        {task && (
          <span className="font-mono text-xs text-muted-foreground">
            · {task.files.length} files · +{task.linesChanged} lines
            {task.sessions > 1 ? ` · ${task.sessions} sessions` : ""}
          </span>
        )}
        <button
          type="button"
          onClick={() => setFollow((value) => !value)}
          className={cn(
            "ml-auto rounded-md border px-2 py-1 text-[11px] transition-colors",
            follow
              ? "border-primary/40 bg-primary/10 text-primary"
              : "border-border text-muted-foreground hover:text-foreground",
          )}
        >
          {follow ? "following" : "paused"}
        </button>
      </div>

      <div className="flex-1 space-y-1.5 overflow-y-auto py-3 pr-1">
        {entries.length === 0 && (
          <p className="px-1 py-6 text-sm text-muted-foreground">
            No transcript yet. The session writes its log as it works.
          </p>
        )}
        {entries.map((entry, index) => (
          <Fragment key={entry.seq}>
            {entry.session !== undefined &&
              entry.session !== entries[index - 1]?.session && (
                <div className="flex items-center gap-2 pt-4 pb-1">
                  <span className="h-px flex-1 bg-border/60" />
                  <span className="font-mono text-[10.5px] uppercase tracking-wider text-muted-foreground/70">
                    session {entry.session}
                    {entry.agent ? ` · ${entry.agent}` : ""}
                  </span>
                  <span className="h-px flex-1 bg-border/60" />
                </div>
              )}
            <Entry
            entry={entry}
            symbols={symbols}
            answers={entries[index - 1]?.tool}
            comments={comments}
            workstream={task?.primary.id ?? workstream}
            onSelect={setPending}
            onResolve={resolve}
            />
          </Fragment>
        ))}
        <div ref={bottom} />
      </div>

      {pending && (
        <div className="border-t border-border/60 pt-3">
          <CommentComposer
            target={pending}
            onSubmit={(body) => post(pending, body)}
            onCancel={() => setPending(null)}
          />
        </div>
      )}
      <div className="hidden">
      </div>
    </div>
  );
}

function Entry({
  entry,
  symbols,
  answers,
  comments,
  workstream,
  onSelect,
  onResolve,
}: {
  entry: LogEntry;
  symbols: Record<string, SymbolInfo>;
  answers?: LogEntry["tool"];
  comments: ReviewComment[];
  workstream: string;
  onSelect: (target: Target) => void;
  onResolve: (id: string) => Promise<void>;
}) {
  const path = entry.tool?.target ?? answers?.target ?? "";
  const commentable = /\.[A-Za-z0-9]+$/.test(path);
  const mine = comments.filter((item) => item.file === path);
  const annotations = mine.map((item) => ({
    side: "additions" as const,
    lineNumber: item.lineStart,
    metadata: { comment: item },
  }));
  const renderComment = (annotation: { metadata: { comment: ReviewComment } }) => (
    <div className="px-3 py-2">
      <CommentCard comment={annotation.metadata.comment} onResolve={onResolve} />
    </div>
  );
  const selectHandler = commentable
    ? (range: { start: number; end: number }) =>
        onSelect({
          file: path,
          lineStart: Math.min(range.start, range.end),
          lineEnd: Math.max(range.start, range.end),
          workstream,
        })
    : undefined;
  if (entry.tool) {
    const Icon = toolIcon(entry.tool.label);
    const coordination = isCoordination(entry.tool.label);
    return (
      <div className="rounded-lg border border-border/50 bg-card/40 px-3 py-2">
        <div className="flex items-center gap-2">
          <Icon
            className={cn("h-3.5 w-3.5 shrink-0", coordination ? "text-primary" : "text-muted-foreground")}
          />
          <span className={cn("text-xs font-medium", coordination && "text-primary")}>
            {entry.tool.label}
          </span>
          {entry.tool.target && (
            <span className="truncate font-mono text-[11.5px] text-muted-foreground">
              {entry.tool.target}
            </span>
          )}
          <span className="ml-auto shrink-0 font-mono text-[10.5px] text-muted-foreground/60">
            {time(entry.at)}
          </span>
        </div>
        {entry.tool.body && (
          <CodeSnippet
            code={entry.tool.body}
            before={entry.tool.before}
            language={entry.tool.language}
            target={entry.tool.target}
            symbols={symbols}
            annotations={annotations}
            renderAnnotation={renderComment}
            onSelectLines={selectHandler}
          />
        )}
      </div>
    );
  }

  if (entry.result) {
    const { ok, preview, lines } = entry.result;
    if (!preview.trim()) return null;
    return (
      <div className="pl-6">
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          {ok ? (
            <CheckCircle2 className="h-3 w-3 text-emerald-500/70" />
          ) : (
            <CircleAlert className="h-3 w-3 text-destructive" />
          )}
          <span>{ok ? "result" : "error"}</span>
          <span className="opacity-60">· {lines} lines</span>
        </div>
        <CodeSnippet
          code={preview}
          language={answers?.language ?? "text"}
          target={answers?.target ?? ""}
          symbols={symbols}
          annotations={annotations}
          renderAnnotation={renderComment}
          onSelectLines={selectHandler}
        />
      </div>
    );
  }

  if (entry.text) {
    return (
      <div className="rounded-lg border border-border/30 bg-background px-3 py-2">
        <div className="mb-1 flex items-center gap-2">
          <Badge variant="secondary" className="h-4 px-1.5 text-[10px]">
            agent
          </Badge>
          <span className="font-mono text-[10.5px] text-muted-foreground/60">{time(entry.at)}</span>
        </div>
        <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground/90">
          {entry.text}
        </p>
      </div>
    );
  }
  return null;
}
