import { spawn } from "node:child_process";
import { mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { resolveRepo } from "@/lib/repos";

export const dynamic = "force-dynamic";

type Agent = {
  title: string;
  intent: string;
  provider: string;
  model: string;
};

type Body = { repo?: string; name?: string; runtime?: string; agents?: Agent[] };

const slug = (text: string, index: number): string =>
  `${String(index + 1).padStart(2, "0")}_${
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 40) || "task"
  }`;

/**
 * Starting a run writes a validated request document and hands it to `lj run`.
 *
 * The alternative -- assembling a command line here -- would put the shape of a run in
 * two places, and the web copy would be the one without validation. The process is
 * detached because a stand outlives the request that started it; progress is read from
 * the ledger afterwards, like everything else.
 */
export async function POST(req: Request) {
  const body = (await req.json()) as Body;
  const repo = resolveRepo(body.repo ?? null);
  const agents = body.agents ?? [];
  if (!repo) return Response.json({ error: "no project selected" }, { status: 400 });

  if (!body.name?.trim()) {
    return Response.json({ error: "a run needs a name" }, { status: 400 });
  }
  if (agents.length === 0) {
    return Response.json({ error: "a run needs at least one agent" }, { status: 400 });
  }
  const empty = agents.findIndex((agent) => !agent.intent?.trim());
  if (empty >= 0) {
    return Response.json(
      { error: `agent ${empty + 1} has no prompt or spec` },
      { status: 400 },
    );
  }

  const request = {
    name: body.name.trim(),
    runtime: body.runtime ?? "claude_code",
    agents: agents.map((agent, index) => ({
      task: {
        task_id: slug(agent.title || agent.intent, index),
        title: (agent.title || `agent ${index + 1}`).slice(0, 200),
        intent: agent.intent.trim(),
        acceptance: [
          "uv run ruff check . passes",
          "uv run ty check passes",
          "uv run pytest passes",
        ],
      },
      provider: agent.provider || "anthropic",
      model: agent.model || "claude-opus-5",
    })),
  };

  const folder = path.join(repo, ".lumberjack", "requests");
  mkdirSync(folder, { recursive: true });
  const file = path.join(folder, `${Date.now()}.json`);
  writeFileSync(file, JSON.stringify(request, null, 2));

  const logs = path.join(repo, ".lumberjack", "logs");
  mkdirSync(logs, { recursive: true });
  const log = path.join(logs, `run-${Date.now()}.log`);
  const handle = openSync(log, "a");
  const child = spawn("uv", ["run", "lj", "run", "--repo", repo, "--request", file], {
    cwd: repo,
    detached: true,
    stdio: ["ignore", handle, handle],
  });
  child.unref();

  // Wait long enough to catch a run that dies on the spot -- a bad request, an expired
  // login, a preflight refusal. Discarding this output is why a run that failed in
  // three seconds looked like a run that was working.
  const died = await new Promise<number | null>((resolve) => {
    const timer = setTimeout(() => resolve(null), 4000);
    child.once("exit", (code) => {
      clearTimeout(timer);
      resolve(code ?? 0);
    });
  });
  if (died !== null && died !== 0) {
    const detail = readFileSync(log, "utf8").trim().slice(-800);
    return Response.json({ error: detail || `lj run exited ${died}`, log }, { status: 409 });
  }

  return Response.json({ ok: true, request: file, agents: request.agents.length, log });
}
