import { execFile } from "node:child_process";
import { readdirSync } from "node:fs";
import type { Dirent } from "node:fs";
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
  truncated?: boolean;
};

type GitStatusName = "added" | "deleted" | "ignored" | "modified" | "renamed" | "untracked";

/**
 * Only two things are hidden from the tree, and neither is a judgement about what
 * matters: `.git` is object storage rather than source, and `node_modules` is 30,000
 * files nobody browses. Everything else appears -- `.lumberjack` above all, since
 * inspecting a run by hand is the whole reason to have a file view here.
 */
const NEVER_WALK = new Set([".git", "node_modules"]);

const MAX_ENTRIES = 40_000;

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
/**
 * The working tree as it exists on disk, not as git indexes it.
 *
 * Reading `git ls-files` was the obvious first implementation and the wrong one: it
 * omits everything gitignored, which is exactly where a Python project keeps `.venv`
 * and where this harness keeps `.lumberjack`. Ignored paths are included and marked,
 * so they read as ignored rather than being absent.
 */
export async function repoTree(repo: string = DEFAULT_REPO): Promise<RepoTree> {
  const [status, ignored] = await Promise.all([
    git(repo, "status", "--porcelain", "-z").catch(() => ""),
    git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
      .catch(() => ""),
  ]);

  const paths: string[] = [];
  const walk = (relative: string): void => {
    if (paths.length > MAX_ENTRIES) return;
    let entries: Dirent[] = [];
    try {
      entries = readdirSync(path.join(repo, relative), { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (NEVER_WALK.has(entry.name)) continue;
      const child = relative ? `${relative}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        paths.push(`${child}/`);
        walk(child);
      } else if (entry.isFile() || entry.isSymbolicLink()) {
        paths.push(child);
      }
    }
  };
  walk("");

  const ignoredPrefixes = ignored.split("\0").filter(Boolean);
  const isIgnored = (file: string): boolean =>
    ignoredPrefixes.some((prefix) =>
      prefix.endsWith("/") ? file.startsWith(prefix) : file === prefix,
    );

  const gitStatus: RepoTree["gitStatus"] = [];
  for (const record of status.split("\0")) {
    if (record.length < 4) continue;
    const code = record.slice(0, 2).trim()[0] ?? "";
    const file = record.slice(3);
    const name = PORCELAIN[code];
    if (name && file) gitStatus.push({ path: file, status: name });
  }
  // Mark the ignored directories themselves rather than every file beneath them: the
  // tree dims a folder and its contents follow, and 8,000 entries do not need rows.
  for (const prefix of ignoredPrefixes) {
    gitStatus.push({ path: prefix.replace(/\/$/, ""), status: "ignored" });
  }

  return { paths: paths.sort(), gitStatus, truncated: paths.length > MAX_ENTRIES };
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
