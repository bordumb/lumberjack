import { addRepo, isGitRepo, listRepos, removeRepo } from "@/lib/repos";
import path from "node:path";

export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ repos: listRepos().map((entry) => ({ ...entry, name: path.basename(entry.path) })) });
}

export async function POST(req: Request) {
  const { path: root } = (await req.json()) as { path?: string };
  if (!root) return Response.json({ error: "path is required" }, { status: 400 });
  if (!(await isGitRepo(root))) {
    return Response.json({ error: `${root} is not a git repository` }, { status: 400 });
  }
  return Response.json({ repo: addRepo(root) });
}

export async function DELETE(req: Request) {
  const { path: root } = (await req.json()) as { path?: string };
  if (!root) return Response.json({ error: "path is required" }, { status: 400 });
  removeRepo(root);
  return Response.json({ ok: true });
}
