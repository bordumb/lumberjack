"use client";

import { useEffect, useRef, useState } from "react";
import { CheckCircle2, CircleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/code-block";
import { isCoordination, toolIcon } from "@/components/tool-icon";
import { cn } from "@/lib/utils";
import type { LogEntry, Workstream } from "@/lib/types";

function time(at: number | null): string {
  return at ? new Date(at).toLocaleTimeString([], { hour12: false }) : "";
}

export function LogStream({ workstream, stand }: { workstream: string; stand: string }) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [agent, setAgent] = useState<Workstream | null>(null);
  const [follow, setFollow] = useState(true);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const source = new EventSource(`/api/agents/${workstream}/log?stand=${stand}`);
    source.onmessage = (event) => {
      const data = JSON.parse(event.data) as { entries: LogEntry[]; workstream: Workstream };
      setEntries(data.entries);
      setAgent(data.workstream);
    };
    return () => source.close();
  }, [workstream, stand]);

  useEffect(() => {
    if (follow) bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries, follow]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-border/60 px-1 pb-3">
        <span className="font-mono text-xs text-muted-foreground">
          {entries.length} entries
        </span>
        {agent && (
          <span className="font-mono text-xs text-muted-foreground">
            · {agent.filesTouched} files touched
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
        {entries.map((entry) => (
          <Entry key={entry.seq} entry={entry} />
        ))}
        <div ref={bottom} />
      </div>
    </div>
  );
}

function Entry({ entry }: { entry: LogEntry }) {
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
        {entry.tool.body && <CodeBlock code={entry.tool.body} language={entry.tool.language} />}
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
        <CodeBlock code={preview} language="text" />
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
