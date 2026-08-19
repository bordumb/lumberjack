import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { DEFAULT_REPO } from "./repos";

const run = promisify(execFile);

export type RepoInfo = {
  name: string;
  root: string;
  branch: string;
  head: string;
  fileCount: number;
};

/** `@pierre/trees` takes a flat path list: directories end with `/`, files do not. */
export type RepoTree = {
  paths: string[];
  gitStatus: { path: string; status: GitStatusName }[];
};

type GitStatusName = "added" | "deleted" | "modified" | "renamed" | "untracked";

const PORCELAIN: Record<string, GitStatusName> = {
  M: "modified",
  A: "added",
  D: "deleted",
  R: "renamed",
  C: "added",
  "?": "untracked",
};

async function git(repo: string, ...args: string[]): Promise<string> {
  const { stdout } = await run("git", args, { cwd: repo, maxBuffer: 64 * 1024 * 1024 });
  return stdout;
}

export async function repoInfo(repo: string = DEFAULT_REPO): Promise<RepoInfo> {
  const [branch, head, files] = await Promise.all([
    git(repo, "rev-parse", "--abbrev-ref", "HEAD").catch(() => "(detached)"),
    git(repo, "rev-parse", "--short", "HEAD").catch(() => "?"),
    git(repo, "ls-files", "-z").catch(() => ""),
  ]);
  return {
    name: path.basename(repo),
    root: repo,
    branch: branch.trim(),
    head: head.trim(),
    fileCount: files.split("\0").filter(Boolean).length,
  };
}

/**
 * The working tree, not a commit.
 *
 * It is what the reader is actually editing, so untracked and modified files belong in
 * it -- shown through git status decorations rather than silently folded in with the
 * committed ones.
 */
export async function repoTree(repo: string = DEFAULT_REPO): Promise<RepoTree> {
  const [tracked, status] = await Promise.all([
    git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
    git(repo, "status", "--porcelain", "-z").catch(() => ""),
  ]);

  const files = tracked.split("\0").filter(Boolean).sort();
  const directories = new Set<string>();
  for (const file of files) {
    const parts = file.split("/");
    for (let depth = 1; depth < parts.length; depth += 1) {
      directories.add(`${parts.slice(0, depth).join("/")}/`);
    }
  }

  const gitStatus: RepoTree["gitStatus"] = [];
  for (const record of status.split("\0")) {
    if (record.length < 4) continue;
    const code = record.slice(0, 2).trim()[0] ?? "";
    const file = record.slice(3);
    const name = PORCELAIN[code];
    if (name && file) gitStatus.push({ path: file, status: name });
  }

  return { paths: [...directories, ...files].sort(), gitStatus };
}

export async function readRepoFile(
  file: string,
  repo: string = DEFAULT_REPO,
): Promise<string | null> {
  // Path traversal would let the browser read anything the dev server can.
  const resolved = path.resolve(repo, file);
  if (!resolved.startsWith(path.resolve(repo))) return null;
  try {
    const { readFile } = await import("node:fs/promises");
    return await readFile(resolved, "utf8");
  } catch {
    return null;
  }
}
