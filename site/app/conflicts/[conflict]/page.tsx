import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ConflictDiffs } from "@/components/conflict-diffs";
import { latestStand, snapshot } from "@/lib/ledger";

export const dynamic = "force-dynamic";

const SEVERITY: Record<string, string> = {
  block: "border-destructive/40 text-destructive",
  warn: "border-amber-500/40 text-amber-400",
  notice: "border-border text-muted-foreground",
};

const EXPLAIN: Record<string, string> = {
  merge_tree: "git performed the merge and it conflicted. This is ground truth.",
  claim_overlap: "the two sides declared overlapping scopes. A prior, not a verdict.",
  symbol_overlap: "both sides changed the same definition.",
  blast_radius: "one side changed a symbol the other transitively depends on.",
  contract_breach: "a frozen interface changed shape.",
};

export default async function ConflictPage({
  params,
  searchParams,
}: {
  params: Promise<{ conflict: string }>;
  searchParams: Promise<{ stand?: string }>;
}) {
  const { conflict: id } = await params;
  const { stand: requested } = await searchParams;
  const stand = requested ?? latestStand();
  const state = stand ? snapshot(stand) : null;
  const conflict = state?.conflicts.find((item) => item.id === id);

  if (!stand || !state || !conflict) notFound();

  const sides = conflict.between.map(
    (workstream) => state.workstreams.find((item) => item.id === workstream) ?? null,
  );

  return (
    <main className="mx-auto max-w-6xl space-y-6 p-8">
      <header className="space-y-2">
        <Link
          href={`/?stand=${stand}`}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3 w-3" />
          all agents
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-sans text-xl font-semibold tracking-[-0.02em]">
            {sides[0]?.title ?? conflict.between[0]} vs {sides[1]?.title ?? conflict.between[1]}
          </h1>
          <Badge variant="outline" className={SEVERITY[conflict.severity]}>
            {conflict.severity}
          </Badge>
          <Badge variant="secondary" className="font-mono text-[10px]">
            {conflict.source}
          </Badge>
        </div>
        <p className="text-sm text-muted-foreground">
          {EXPLAIN[conflict.source] ?? "the oracle raised this."}
        </p>
        <p className="font-mono text-[11px] text-muted-foreground/70">{conflict.id}</p>
      </header>

      {conflict.evidence && (
        <section>
          <h2 className="mb-1.5 font-sans text-sm font-medium tracking-[-0.01em]">Evidence</h2>
          <pre className="overflow-x-auto rounded-lg border border-border/60 bg-muted/40 px-3 py-2 font-mono text-[11.5px] leading-relaxed text-foreground/85">
            {conflict.evidence}
          </pre>
        </section>
      )}

      <section>
        <h2 className="mb-2 font-sans text-sm font-medium tracking-[-0.01em]">
          Contested files ({conflict.paths.length})
        </h2>
        <ConflictDiffs conflict={conflict.id} stand={stand} />
      </section>
    </main>
  );
}
