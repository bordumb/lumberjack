import { readRepoFile } from "@/lib/repo";
import { resolveRepo } from "@/lib/repos";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const repo = resolveRepo(params.get("repo"));
  const file = params.get("path");
  if (!repo) return Response.json({ error: "no project" }, { status: 404 });
  if (!file) return Response.json({ error: "path is required" }, { status: 400 });
  const contents = await readRepoFile(file, repo);
  if (contents === null) return Response.json({ error: "not found" }, { status: 404 });
  return Response.json({ path: file, contents });
}
