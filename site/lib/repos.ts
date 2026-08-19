import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);

/**
 * Which repositories the dashboard knows about.
 *
 * This is the one thing here that cannot be recomputed: a list of paths a person
 * chose. Everything else on screen is derived from git or from a stand's ledger, so
 * this file is deliberately the only stored state, and it is config rather than a
 * cache of anything.
 */
const REGISTRY = path.join(os.homedir(), ".lumberjack", "repos.json");

export type RegisteredRepo = { path: string; addedAt: number };

export const DEFAULT_REPO = path.resolve(
  process.env.LUMBERJACK_REPO ?? path.join(process.cwd(), ".."),
);

function read(): RegisteredRepo[] {
  try {
    const parsed = JSON.parse(readFileSync(REGISTRY, "utf8")) as { repos?: RegisteredRepo[] };
    return parsed.repos ?? [];
  } catch {
    return [];
  }
}

function write(repos: RegisteredRepo[]): void {
  mkdirSync(path.dirname(REGISTRY), { recursive: true });
  writeFileSync(REGISTRY, JSON.stringify({ repos }, null, 2));
}

/**
 * The registry is the truth, seeded once.
 *
 * The repository the dashboard was started in used to be pinned and unremovable, which
 * made it the one project a person could not tidy away. It is now simply the first
 * entry, written on first use so an empty dashboard still has somewhere to start.
 */
export function listRepos(): RegisteredRepo[] {
  if (!existsSync(REGISTRY)) {
    write([{ path: DEFAULT_REPO, addedAt: Date.now() }]);
  }
  return read();
}

export function addRepo(root: string): RegisteredRepo {
  const resolved = path.resolve(root);
  // Through listRepos, so seeding happens once and a first write cannot erase it.
  const repos = listRepos().filter((entry) => entry.path !== resolved);
  const entry = { path: resolved, addedAt: Date.now() };
  write([...repos, entry]);
  return entry;
}

export function removeRepo(root: string): void {
  const resolved = path.resolve(root);
  write(listRepos().filter((entry) => entry.path !== resolved));
}

/** A path the browser asked for is only usable if it is actually a git repository. */
export async function isGitRepo(root: string): Promise<boolean> {
  try {
    const { stdout } = await run("git", ["rev-parse", "--is-inside-work-tree"], { cwd: root });
    return stdout.trim() === "true";
  } catch {
    return false;
  }
}

/**
 * Which repository a request is about, or `null` when there is none.
 *
 * Returning a default when nothing is registered is what let the file tree keep serving
 * a project the reader had just removed: the nav had let it go and every other surface
 * had not. "No project" is a state the interface has to be able to be in, so it is a
 * value the type admits rather than a case the fallback hides.
 */
export function resolveRepo(requested: string | null): string | null {
  const known = listRepos();
  if (requested) {
    const resolved = path.resolve(requested);
    if (known.some((entry) => entry.path === resolved)) return resolved;
  }
  return known[0]?.path ?? null;
}

export type DirEntry = { name: string; path: string; isRepo: boolean };

/**
 * A server-side folder browser, because the browser's own directory pickers withhold
 * the absolute path and git needs one. The dashboard is local by construction, so the
 * server reading its own filesystem is the same trust boundary the terminal has.
 */
export function browse(at: string | null): { cwd: string; parent: string | null; entries: DirEntry[] } {
  const cwd = at ? path.resolve(at) : os.homedir();
  let names: string[] = [];
  try {
    names = readdirSync(cwd, { withFileTypes: true })
      // Dotfolders are shown here as well; a repository can live inside one.
      .filter((entry) => entry.isDirectory() && entry.name !== ".git")
      .map((entry) => entry.name)
      .sort();
  } catch {
    names = [];
  }
  const entries = names.map((name) => {
    const full = path.join(cwd, name);
    return { name, path: full, isRepo: existsSync(path.join(full, ".git")) };
  });
  const parent = path.dirname(cwd);
  return { cwd, parent: parent === cwd ? null : parent, entries };
}
