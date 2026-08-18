import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { LogStream } from "@/components/log-stream";
import { latestStand, snapshot } from "@/lib/ledger";

export const dynamic = "force-dynamic";

export default async function AgentPage({
  params,
  searchParams,
}: {
  params: Promise<{ workstream: string }>;
  searchParams: Promise<{ stand?: string }>;
}) {
  const { workstream } = await params;
  const { stand: requested } = await searchParams;
  const stand = requested ?? latestStand();
  const state = stand ? snapshot(stand) : null;
  const agent = state?.workstreams.find((item) => item.id === workstream);

  if (!stand || !agent) notFound();

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
          <h1 className="font-sans text-xl font-semibold tracking-[-0.02em]">{agent.title}</h1>
          <Badge variant="outline" className="font-mono text-[10px]">
            {agent.state}
          </Badge>
          <span className="font-mono text-xs text-muted-foreground">{agent.agent}</span>
        </div>
        <p className="font-mono text-[11px] text-muted-foreground">
          {agent.branch} · {agent.worktree}
        </p>
      </header>

      <div className="min-h-0 flex-1">
        <LogStream workstream={agent.id} stand={stand} />
      </div>
    </main>
  );
}
