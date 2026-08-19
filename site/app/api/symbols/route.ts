import { latestStand, snapshot } from "@/lib/ledger";
import { repoDefinitions } from "@/lib/git";
import { resolveRepo } from "@/lib/repos";

export const dynamic = "force-dynamic";

/** Repo-wide definitions, so token hover works in the agent log too. */
export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const repo = resolveRepo(params.get("repo"));
  if (!repo) return Response.json({});
  const stand = params.get("stand") ?? latestStand(repo);
  const state = stand ? snapshot(stand, repo) : null;
  const rev = state?.integrationHead ?? "HEAD";
  const found = await repoDefinitions(rev, repo);

  return Response.json(
    Object.fromEntries(
      Object.entries(found).map(([name, entry]) => [
        name,
        {
          name,
          kind: entry.text.startsWith("class") ? "class" : "function",
          signature: `${entry.text.replace(/:$/, "")}   ·   ${entry.file}`,
          line: entry.line,
        },
      ]),
    ),
  );
}
