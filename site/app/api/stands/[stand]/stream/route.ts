import { latestStand, snapshot } from "@/lib/ledger";

export const dynamic = "force-dynamic";

/** Server-sent events. Polling the ledger is cheap and survives the writer being
 *  a different process, which a filesystem watcher on a WAL database does not. */
export async function GET(_req: Request, ctx: { params: Promise<{ stand: string }> }) {
  const { stand: requested } = await ctx.params;
  const stand = requested === "latest" ? latestStand() : requested;

  const encoder = new TextEncoder();
  let timer: ReturnType<typeof setInterval> | undefined;

  const stream = new ReadableStream({
    start(controller) {
      let previous = "";
      const push = () => {
        if (!stand) return;
        const data = snapshot(stand);
        const serialized = JSON.stringify(data);
        if (serialized === previous) return;
        previous = serialized;
        controller.enqueue(encoder.encode(`data: ${serialized}\n\n`));
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
