import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { latestStand, REPO } from "@/lib/ledger";

const run = promisify(execFile);

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const { comment, stand } = (await req.json()) as { comment?: string; stand?: string };
  const target = stand ?? latestStand();
  if (!target || !comment) {
    return Response.json({ error: "stand and comment are required" }, { status: 400 });
  }
  try {
    await run("uv", ["run", "lj", "resolve", comment, "--repo", REPO, "--stand", target], {
      cwd: REPO,
    });
    return Response.json({ ok: true });
  } catch (cause) {
    const detail = String((cause as { stderr?: string }).stderr ?? cause);
    return Response.json({ error: detail.slice(-800) }, { status: 500 });
  }
}
