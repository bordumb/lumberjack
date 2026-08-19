"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Activity, AlertTriangle, FileDiff, Lock, ShieldAlert, Timer } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { StandSnapshot } from "@/lib/types";
import { weave } from "@/lib/weave";
import { StandControls } from "@/components/stand-controls";

const LIFECYCLE: Record<string, string> = {
  live: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  stale: "bg-orange-500/15 text-orange-400 border-orange-500/30",
  halted: "bg-destructive/15 text-destructive border-destructive/30",
  finished: "bg-amber-500/15 text-amber-400 border-amber-500/30",
};

const STATE: Record<string, string> = {
  running: "text-emerald-400",
  awaiting_integration: "text-sky-400",
  landed: "text-primary",
  blocked: "text-destructive",
  assigned: "text-muted-foreground",
};

function elapsed(from: number | null): string {
  if (!from) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - from) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function Dashboard({ stand, repo }: { stand: string; repo?: string | null }) {
  const [data, setData] = useState<StandSnapshot | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const source = new EventSource(`/api/stands/${stand}/stream`);
    source.onmessage = (event) => setData(JSON.parse(event.data) as StandSnapshot);
    return () => source.close();
  }, [stand]);

  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  void tick;

  if (!data) {
    return <p className="p-8 text-sm text-muted-foreground">Connecting to the ledger…</p>;
  }

  // Everything below reads tasks, never lanes. A task that has been worked on across
  // sessions has several lanes, and reading one of them shows a fraction of the work.
  const tasks = weave(data);

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-8">
      <header className="space-y-2">
        <div className="flex items-start gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <h1 className="font-sans text-xl font-semibold tracking-[-0.02em]">
              {data.title || data.stand}
            </h1>
            <span
              className={cn(
                "rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
                LIFECYCLE[data.lifecycle],
              )}
            >
              {data.lifecycle}
            </span>
          </div>
          <div className="ml-auto shrink-0">
            <StandControls
              stand={data.stand}
              repo={repo ?? null}
              lifecycle={data.lifecycle}
              title={data.title || data.stand}
              onChanged={() => setTick((value) => value + 1)}
            />
          </div>
        </div>
        <p className="font-mono text-xs text-muted-foreground">
          {data.integrationBranch} @ {data.integrationHead?.slice(0, 8) ?? "—"} ·{" "}
          {data.totalEvents} events · running {elapsed(data.startedAt)}
        </p>
      </header>

      <section className="grid gap-3 sm:grid-cols-4">
        <Stat icon={Activity} label="tasks" value={tasks.length} />
        <Stat
          icon={FileDiff}
          label="files touched"
          value={new Set(tasks.flatMap((task) => task.files)).size}
        />
        <Stat
          icon={Lock}
          label="leases held"
          value={tasks.reduce((total, task) => total + task.leases.length, 0)}
        />
        <Stat
          icon={AlertTriangle}
          label="open conflicts"
          value={data.conflicts.length}
          tone={data.conflicts.length > 0 ? "warn" : undefined}
        />
      </section>

      <section className="space-y-2">
        {tasks.map((task) => (
          <Link
            key={task.task}
            href={`/agents/${task.primary.id}?stand=${data.stand}`}
            className="block"
          >
            <Card className="gap-0 border-border/60 p-4 transition-colors hover:border-primary/40 hover:bg-card/80">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span
                  className={cn(
                    "font-sans text-sm font-medium tracking-[-0.01em]",
                    STATE[task.state] ?? "",
                  )}
                >
                  {task.title}
                </span>
                <Badge variant="outline" className="h-5 font-mono text-[10px]">
                  {task.state}
                </Badge>
                {task.sessions > 1 && (
                  <Badge variant="secondary" className="h-5 font-mono text-[10px]">
                    {task.sessions} sessions
                  </Badge>
                )}
                {task.violations > 0 && (
                  <span className="flex items-center gap-1 text-[11px] text-amber-400">
                    <ShieldAlert className="h-3 w-3" />
                    {task.violations}
                  </span>
                )}
                <span className="ml-auto truncate font-mono text-[11px] text-muted-foreground">
                  {task.agents.join(" · ")}
                </span>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-muted-foreground">
                <span className="flex items-center gap-1">
                  <FileDiff className="h-3 w-3" />
                  {task.files.length} files
                </span>
                <span>+{task.linesChanged} lines</span>
                <span className="flex items-center gap-1">
                  <Lock className="h-3 w-3" />
                  {task.leases.length} leases
                </span>
                <span>{task.claims} claims</span>
                <span className="flex items-center gap-1">
                  <Timer className="h-3 w-3" />
                  {elapsed(task.lastActivity)} since activity
                </span>
              </div>

              {task.files.length > 0 && (
                <p className="mt-2 truncate font-mono text-[11px] text-muted-foreground/70">
                  {task.files.slice(0, 8).join(" · ")}
                </p>
              )}
            </Card>
          </Link>
        ))}
      </section>

      {data.conflicts.length > 0 && (
        <section className="space-y-2">
          <h2 className="font-sans text-sm font-medium tracking-[-0.01em]">Open conflicts</h2>
          {data.conflicts.map((conflict) => (
            <Link
              key={conflict.id}
              href={`/conflicts/${conflict.id}?stand=${data.stand}`}
              className="block"
            >
              <Card className="gap-1 border-amber-500/30 bg-amber-500/5 p-3 transition-colors hover:border-amber-500/60 hover:bg-amber-500/10">
              <div className="flex items-center gap-2 text-xs">
                <Badge variant="outline" className="h-5 border-amber-500/40 text-amber-400">
                  {conflict.severity}
                </Badge>
                <span className="font-mono text-muted-foreground">{conflict.source}</span>
                <span className="font-mono text-muted-foreground/70">
                  {conflict.between.join(" vs ")}
                </span>
              </div>
                {conflict.paths.length > 0 && (
                  <p className="font-mono text-[11px] text-muted-foreground">
                    {conflict.paths.join(", ")}
                  </p>
                )}
              </Card>
            </Link>
          ))}
        </section>
      )}

      {data.notes.length > 0 && (
        <section className="space-y-2">
          <h2 className="font-sans text-sm font-medium tracking-[-0.01em]">Blackboard</h2>
          {data.notes.slice(0, 8).map((note) => (
            <Link
              key={note.id}
              href={`/notes/${note.id}?stand=${data.stand}`}
              className="block"
            >
              <Card className="gap-1 border-border/50 p-3 transition-colors hover:border-primary/40 hover:bg-card/80">
                <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-muted-foreground">
                  <Badge variant="secondary" className="h-4 px-1.5 text-[10px]">
                    {note.topic}
                  </Badge>
                  {note.author}
                  {note.scope.length > 0 && (
                    <span className="truncate opacity-60">{note.scope.join(", ")}</span>
                  )}
                </div>
                <p className="line-clamp-2 text-[12.5px] text-foreground/85">{note.body}</p>
              </Card>
            </Link>
          ))}
        </section>
      )}
    </div>
  );
}

function Stat({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: number;
  tone?: "warn";
}) {
  return (
    <Card className="gap-1 border-border/60 p-4">
      <div className="flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      <p className={cn(
          "font-sans text-[28px] font-semibold leading-none tracking-[-0.03em] tabular-nums",
          tone === "warn" && "text-amber-400",
        )}>
        {value}
      </p>
    </Card>
  );
}
