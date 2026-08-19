import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { resolveRepo } from "@/lib/repos";

const run = promisify(execFile);

export const dynamic = "force-dynamic";

type Action = "pause" | "continue" | "rename" | "delete";
type Body = { action?: Action; repo?: string; name?: string; force?: boolean; dropBranches?: boolean };

/**
 * Operating a stand goes through `lj`, not through the ledger directly.
 *
 * Pausing writes an event, continuing starts a whole new run, and deleting inspects
 * what it would destroy before destroying it. All three have rules, and the rules live
 * in one place rather than being re-stated here in a form nobody tests.
 */
export async function POST(req: Request, ctx: { params: Promise<{ stand: string }> }) {
  const { stand } = await ctx.params;
  const body = (await req.json()) as Body;
  const repo = resolveRepo(body.repo ?? null);
  const base = ["run", "lj"];

  try {
    switch (body.action) {
      case "pause": {
        await run("uv", [...base, "halt", "--repo", repo, "--stand", stand], { cwd: repo });
        return Response.json({ ok: true, state: "halted" });
      }

      case "continue": {
        // Not a resumption: halting killed the sessions and the supervisor exited.
        // This starts a new stand whose worktrees begin on this one's branches.
        const child = spawn("uv", [...base, "run", "--repo", repo, "--resume", stand], {
          cwd: repo,
          detached: true,
          stdio: "ignore",
        });
        child.unref();
        return Response.json({ ok: true, state: "continuing" });
      }

      case "rename": {
        if (!body.name?.trim()) {
          return Response.json({ error: "a name is required" }, { status: 400 });
        }
        await run("uv", [...base, "rename", body.name.trim(), "--repo", repo, "--stand", stand], {
          cwd: repo,
        });
        return Response.json({ ok: true, name: body.name.trim() });
      }

      case "delete": {
        const args = [...base, "delete", "--repo", repo, "--stand", stand];
        if (body.force) args.push("--force");
        if (body.dropBranches) args.push("--drop-branches");
        const { stdout } = await run("uv", args, { cwd: repo });
        return Response.json({ ok: true, detail: stdout.trim() });
      }

      default:
        return Response.json({ error: `unknown action ${body.action}` }, { status: 400 });
    }
  } catch (cause) {
    const error = cause as { stdout?: string; stderr?: string };
    // `lj delete` refuses and explains on stdout when work would be lost.
    const detail = (error.stdout || error.stderr || String(cause)).trim();
    return Response.json({ error: detail.slice(-800) }, { status: 409 });
  }
}
