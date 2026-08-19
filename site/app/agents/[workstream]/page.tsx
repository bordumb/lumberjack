import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { LogStream } from "@/components/log-stream";
import { latestStand, snapshot } from "@/lib/ledger";
import { resolveRepo } from "@/lib/repos";
import { taskOfLane } from "@/lib/weave";

export const dynamic = "force-dynamic";

export default async function AgentPage({
  params,
  searchParams,
}: {
  params: Promise<{ workstream: string }>;
  searchParams: Promise<{ stand?: string; repo?: string }>;
}) {
  const { workstream } = await params;
  const { stand: requested, repo: requestedRepo } = await searchParams;
  const repo = resolveRepo(requestedRepo ?? null);
  const stand = requested ?? latestStand(repo);
  const state = stand ? snapshot(stand, repo) : null;
  const task = state ? taskOfLane(state, workstream) : undefined;

  if (!stand || !task) notFound();

  return (
    <main className="mx-auto flex h-screen max-w-5xl flex-col p-8">
      <header className="space-y-2 pb-4">
        <Link
          href={`/?stand=${stand}`}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3 w-3" />
          all agents
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-sans text-xl font-semibold tracking-[-0.02em]">{task.title}</h1>
          <Badge variant="outline" className="font-mono text-[10px]">
            {task.state}
          </Badge>
          {task.sessions > 1 && (
            <Badge variant="secondary" className="font-mono text-[10px]">
              {task.sessions} sessions
            </Badge>
          )}
          <span className="font-mono text-xs text-muted-foreground">
            {task.agents.join(" · ")}
          </span>
        </div>
        <p className="font-mono text-[11px] text-muted-foreground">
          {task.branch} · {task.files.length} files · +{task.linesChanged} lines
        </p>
      </header>

      <div className="min-h-0 flex-1">
        <LogStream workstream={task.primary.id} stand={stand} />
      </div>
    </main>
  );
}
