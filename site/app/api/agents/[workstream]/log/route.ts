import { latestStand, snapshot, transcriptFile } from "@/lib/ledger";
import { resolveRepo } from "@/lib/repos";
import { parseTranscript } from "@/lib/transcript";
import { taskOfLane } from "@/lib/weave";
import type { LogEntry } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Every session's transcript for one task, in one stream.
 *
 * Claude Code keeps a log per working directory, and a task picked up again gets a new
 * worktree -- so reading the current one shows only what the latest session did. The
 * lanes are read in order and merged, with each entry carrying the session it came
 * from so the seam stays visible rather than being smoothed over.
 */
export async function GET(req: Request, ctx: { params: Promise<{ workstream: string }> }) {
  const { workstream } = await ctx.params;
  const params = new URL(req.url).searchParams;
  const repo = resolveRepo(params.get("repo"));
  const stand = params.get("stand") ?? latestStand(repo);
  const state = stand ? snapshot(stand, repo) : null;
  const task = state ? taskOfLane(state, workstream) : undefined;

  if (!task) return new Response("unknown workstream", { status: 404 });

  const encoder = new TextEncoder();
  let timer: ReturnType<typeof setInterval> | undefined;

  const collect = (): LogEntry[] => {
    // Oldest lane first, so the reader moves forward through the task's history.
    const ordered = [...task.lanes].reverse();
    const merged: LogEntry[] = [];
    ordered.forEach((lane, index) => {
      const file = transcriptFile(lane.worktree);
      if (!file) return;
      for (const entry of parseTranscript(file, lane.worktree)) {
        merged.push({ ...entry, session: index + 1, lane: lane.id, agent: lane.agent });
      }
    });
    return merged.map((entry, index) => ({ ...entry, seq: index }));
  };

  const stream = new ReadableStream({
    start(controller) {
      let previous = 0;
      const push = () => {
        const entries = collect();
        if (entries.length === previous) return;
        previous = entries.length;
        controller.enqueue(
          encoder.encode(`data: ${JSON.stringify({ task, entries })}\n\n`),
        );
      };
      push();
      timer = setInterval(push, 1000);
    },
    cancel() {
      if (timer) clearInterval(timer);
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}
