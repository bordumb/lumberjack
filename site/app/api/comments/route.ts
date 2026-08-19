import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { latestStand, snapshot, REPO } from "@/lib/ledger";

const run = promisify(execFile);

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const stand = new URL(req.url).searchParams.get("stand") ?? latestStand();
  const state = stand ? snapshot(stand) : null;
  return Response.json({ comments: state?.comments ?? [] });
}

type Posted = {
  stand?: string;
  body?: string;
  file?: string;
  line?: number;
  lineEnd?: number;
  side?: string;
  workstream?: string;
  conflict?: string;
};

/**
 * Posting shells out to `lj comment` rather than writing the ledger from here.
 *
 * The event has a validated shape and a routing rule -- who hears about it depends on
 * whether it was left on a workstream or a conflict -- and both live in the Python
 * domain. Reimplementing them in the dashboard would mean two definitions of the same
 * thing, drifting apart.
 */
export async function POST(req: Request) {
  const input = (await req.json()) as Posted;
  const stand = input.stand ?? latestStand();

  if (!stand || !input.body?.trim() || !input.file || !input.line) {
    return Response.json({ error: "stand, body, file and line are required" }, { status: 400 });
  }

  const args = [
    "run", "lj", "comment", input.body,
    "--repo", REPO,
    "--stand", stand,
    "--file", input.file,
    "--line", String(input.line),
  ];
  if (input.lineEnd && input.lineEnd !== input.line) args.push("--line-end", String(input.lineEnd));
  if (input.side) args.push("--side", input.side);
  if (input.workstream) args.push("--workstream", input.workstream);
  if (input.conflict) args.push("--conflict", input.conflict);

  try {
    const { stdout } = await run("uv", args, { cwd: REPO, maxBuffer: 4 * 1024 * 1024 });
    return Response.json({ ok: true, detail: stdout.trim() });
  } catch (cause) {
    const detail = String((cause as { stderr?: string }).stderr ?? cause);
    return Response.json({ error: detail.slice(-800) }, { status: 500 });
  }
}
