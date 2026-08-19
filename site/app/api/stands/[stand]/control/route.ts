import { execFile, spawn } from "node:child_process";
import { mkdirSync, openSync, readFileSync } from "node:fs";
import path from "node:path";
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
        // The same stand gets another session -- same ledger, same task ids, same
        // branches. Forking a new run on every pause would make the unit of analysis
        // the process rather than the work.
        //
        // The output goes to a file rather than /dev/null, and we wait a moment to
        // see whether it dies on the spot. A detached process whose failure nobody
        // reads is how "I clicked it and nothing happened" happens.
        const logs = path.join(repo, ".lumberjack", "logs");
        mkdirSync(logs, { recursive: true });
        const log = path.join(logs, `resume-${stand}-${Date.now()}.log`);
        const handle = openSync(log, "a");
        const child = spawn("uv", [...base, "resume", "--repo", repo, "--stand", stand], {
          cwd: repo,
          detached: true,
          stdio: ["ignore", handle, handle],
        });
        child.unref();

        const died = await new Promise<number | null>((resolve) => {
          const timer = setTimeout(() => resolve(null), 4000);
          child.once("exit", (code) => {
            clearTimeout(timer);
            resolve(code ?? 0);
          });
        });
        if (died !== null && died !== 0) {
          const detail = readFileSync(log, "utf8").trim().slice(-800);
          return Response.json(
            { error: detail || `lj run exited ${died}`, log },
            { status: 409 },
          );
        }
        return Response.json({ ok: true, state: "continuing", log });
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
