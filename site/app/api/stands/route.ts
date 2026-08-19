import { latestStand, listStands, snapshot } from "@/lib/ledger";
import { resolveRepo } from "@/lib/repos";

export const dynamic = "force-dynamic";

/**
 * The nav shows a run, not an id. A hex string tells the reader nothing about which
 * run it was or whether it is still going, so the goal and lifecycle come with it.
 */
export function GET(req: Request) {
  const repo = resolveRepo(new URL(req.url).searchParams.get("repo"));
  const stands = listStands(repo).map((entry) => {
    const state = snapshot(entry.stand, repo);
    return {
      ...entry,
      goal: state?.goal ?? "",
      lifecycle: state?.lifecycle ?? "finished",
      workstreams: state?.workstreams.length ?? 0,
      conflicts: state?.conflicts.length ?? 0,
      comments: state?.comments.filter((item) => !item.resolved).length ?? 0,
    };
  });
  return Response.json({ stands, latest: latestStand(repo), repo });
}
