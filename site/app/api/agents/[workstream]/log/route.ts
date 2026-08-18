import { latestStand, snapshot, transcriptFile } from "@/lib/ledger";
import { parseTranscript } from "@/lib/transcript";

export const dynamic = "force-dynamic";

export async function GET(req: Request, ctx: { params: Promise<{ workstream: string }> }) {
  const { workstream } = await ctx.params;
  const stand = new URL(req.url).searchParams.get("stand") ?? latestStand();
  const state = stand ? snapshot(stand) : null;
  const found = state?.workstreams.find((item) => item.id === workstream);

  if (!found) return new Response("unknown workstream", { status: 404 });

  const encoder = new TextEncoder();
  let timer: ReturnType<typeof setInterval> | undefined;

  const stream = new ReadableStream({
    start(controller) {
      let previous = 0;
      const push = () => {
        const file = transcriptFile(found.worktree);
        const entries = file ? parseTranscript(file, found.worktree) : [];
        if (entries.length === previous) return;
        previous = entries.length;
        controller.enqueue(
          encoder.encode(`data: ${JSON.stringify({ workstream: found, entries })}\n\n`),
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
