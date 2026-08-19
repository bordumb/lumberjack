import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, FileCode2, Pin, Users } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { NoteBody } from "@/components/note-body";
import { latestStand, snapshot } from "@/lib/ledger";
import { resolveRepo } from "@/lib/repos";

export const dynamic = "force-dynamic";

const TOPIC: Record<string, string> = {
  decisions: "a decision the next agent should not relitigate",
  conventions: "a convention for everyone working here",
  gotchas: "something that will bite whoever touches this next",
  handoff: "an account of what a session did",
};

function when(at: number): string {
  return new Date(at).toLocaleString([], { hour12: false });
}

export default async function NotePage({
  params,
  searchParams,
}: {
  params: Promise<{ note: string }>;
  searchParams: Promise<{ stand?: string; repo?: string }>;
}) {
  const { note: id } = await params;
  const { stand: requested, repo: requestedRepo } = await searchParams;
  const repo = resolveRepo(requestedRepo ?? null);
  if (!repo) notFound();
  const stand = requested ?? latestStand(repo);
  const state = stand ? snapshot(stand, repo) : null;
  const note = state?.notes.find((item) => item.id === id);

  if (!stand || !state || !note) notFound();

  const query = requestedRepo ? `?repo=${encodeURIComponent(requestedRepo)}&stand=${stand}` : `?stand=${stand}`;
  const author = state.workstreams.find((item) => item.agent === note.author);
  const siblings = state.notes.filter(
    (item) => item.topic === note.topic && item.id !== note.id,
  );

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-8">
      <header className="space-y-2">
        <Link
          href={`/${query}`}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3 w-3" />
          {state.title}
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" className="text-[10px]">
            {note.topic}
          </Badge>
          {author ? (
            <Link
              href={`/agents/${author.id}${query}`}
              className="font-mono text-[11.5px] text-primary hover:underline"
            >
              {note.author}
            </Link>
          ) : (
            <span className="font-mono text-[11.5px] text-muted-foreground">{note.author}</span>
          )}
          <span className="font-mono text-[11px] text-muted-foreground/60">{when(note.at)}</span>
        </div>
        <p className="text-[12.5px] text-muted-foreground">
          {TOPIC[note.topic] ?? "posted to the blackboard"}
        </p>
      </header>

      {(note.scope.length > 0 || note.pins.length > 0) && (
        <section className="space-y-2 rounded-lg border border-border/60 bg-card/40 p-3">
          {note.scope.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <FileCode2 className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="font-mono text-[11px] text-muted-foreground">reaches</span>
              {note.scope.map((pattern) => (
                <code
                  key={pattern}
                  className="rounded bg-muted/60 px-1.5 py-0.5 font-mono text-[11px]"
                >
                  {pattern}
                </code>
              ))}
            </div>
          )}
          {note.pins.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Pin className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              {note.pins.map((pin) => (
                <code
                  key={pin}
                  className="rounded bg-muted/60 px-1.5 py-0.5 font-mono text-[11px]"
                >
                  {pin}
                </code>
              ))}
            </div>
          )}
          {note.scope.length === 0 && (
            <p className="flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
              <Users className="h-3.5 w-3.5" />
              unscoped, so it competes for every agent&apos;s context budget
            </p>
          )}
        </section>
      )}

      <NoteBody body={note.body} />

      {siblings.length > 0 && (
        <section className="space-y-2 border-t border-border/60 pt-5">
          <h2 className="font-sans text-sm font-medium tracking-[-0.01em]">
            Also under {note.topic}
          </h2>
          {siblings.map((item) => (
            <Link
              key={item.id}
              href={`/notes/${item.id}${query}`}
              className="block rounded-lg border border-border/50 p-3 transition-colors hover:border-primary/40"
            >
              <div className="mb-1 flex items-center gap-2 font-mono text-[10.5px] text-muted-foreground">
                {item.author}
                <span className="opacity-60">{when(item.at)}</span>
              </div>
              <p className="line-clamp-2 text-[12.5px] text-foreground/80">{item.body}</p>
            </Link>
          ))}
        </section>
      )}
    </main>
  );
}
