import { repoInfo } from "@/lib/repo";
import { resolveRepo } from "@/lib/repos";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const repo = resolveRepo(new URL(req.url).searchParams.get("repo"));
  return Response.json(await repoInfo(repo));
}
