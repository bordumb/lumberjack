import { repoTree } from "@/lib/repo";
import { resolveRepo } from "@/lib/repos";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const repo = resolveRepo(new URL(req.url).searchParams.get("repo"));
  // An empty tree, not the tree of some other project the reader did not ask for.
  if (!repo) return Response.json({ paths: [], gitStatus: [] });
  return Response.json(await repoTree(repo));
}
