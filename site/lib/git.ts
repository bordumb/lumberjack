import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { DEFAULT_REPO } from "./repos";

const run = promisify(execFile);

/** Read a file as it exists at a revision. Returns null when it is absent there. */
export async function showFile(
  rev: string,
  file: string,
  repo: string = DEFAULT_REPO,
): Promise<string | null> {
  try {
    const { stdout } = await run("git", ["show", `${rev}:${file}`], {
      cwd: repo,
      maxBuffer: 32 * 1024 * 1024,
    });
    return stdout;
  } catch {
    return null;
  }
}

export async function mergeBase(
  left: string,
  right: string,
  repo: string = DEFAULT_REPO,
): Promise<string | null> {
  try {
    const { stdout } = await run("git", ["merge-base", left, right], { cwd: repo });
    return stdout.trim() || null;
  } catch {
    return null;
  }
}

export async function resolveRef(
  ref: string,
  repo: string = DEFAULT_REPO,
): Promise<string | null> {
  try {
    const { stdout } = await run("git", ["rev-parse", "--verify", `${ref}^{commit}`], {
      cwd: repo,
    });
    return stdout.trim() || null;
  } catch {
    return null;
  }
}


/**
 * Produce the file as git itself would leave it mid-merge: three-way merged, with
 * conflict markers where the two sides disagree. Exit status is the conflict count,
 * so a clean merge is distinguishable from a conflicted one without parsing.
 */
export async function mergeWithMarkers(
  base: string,
  ours: string,
  theirs: string,
  labels: { ours: string; theirs: string },
  repo: string = DEFAULT_REPO,
): Promise<{ merged: string; conflicted: boolean }> {
  const dir = await mkdtemp(path.join(tmpdir(), "lj-merge-"));
  try {
    const files = {
      base: path.join(dir, "base"),
      ours: path.join(dir, "ours"),
      theirs: path.join(dir, "theirs"),
    };
    await Promise.all([
      writeFile(files.base, base),
      writeFile(files.ours, ours),
      writeFile(files.theirs, theirs),
    ]);
    try {
      const { stdout } = await run(
        "git",
        [
          "merge-file", "-p", "--diff3",
          "-L", labels.ours, "-L", "base", "-L", labels.theirs,
          files.ours, files.base, files.theirs,
        ],
        { cwd: repo, maxBuffer: 32 * 1024 * 1024 },
      );
      return { merged: stdout, conflicted: false };
    } catch (cause) {
      // A non-zero exit is the conflict count, and stdout still holds the merge.
      const merged = String((cause as { stdout?: string }).stdout ?? "");
      return { merged, conflicted: merged.includes("<<<<<<<") };
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}


const symbolCache = new Map<string, Record<string, { file: string; line: number; text: string }>>();

/**
 * Every definition in the tree at a revision, in one `git grep`.
 *
 * Hover is most useful on names a file *imports* -- that is the moment you cannot see
 * the signature -- so indexing only the file under the cursor misses the point.
 */
export async function repoDefinitions(
  rev: string,
  repo: string = DEFAULT_REPO,
): Promise<Record<string, { file: string; line: number; text: string }>> {
  const cached = symbolCache.get(`${repo}:${rev}`);
  if (cached) return cached;

  const found: Record<string, { file: string; line: number; text: string }> = {};
  try {
    const { stdout } = await run(
      "git",
      [
        "grep", "-n", "-E",
        "^[[:space:]]*(async def |def |class )",
        rev, "--", "*.py",
      ],
      { cwd: repo, maxBuffer: 64 * 1024 * 1024 },
    );
    for (const line of stdout.split("\n")) {
      // <rev>:<path>:<lineno>:<text>
      const match = /^[^:]+:([^:]+):(\d+):(.*)$/.exec(line);
      if (!match) continue;
      const [, file, lineNumber, text] = match;
      const name = /(?:async def |def |class )([A-Za-z_]\w*)/.exec(text)?.[1];
      if (!name || found[name]) continue;
      found[name] = { file, line: Number(lineNumber), text: text.trim() };
    }
  } catch {
    // An empty index is a hover that says nothing, which is the correct failure here.
  }
  symbolCache.set(`${repo}:${rev}`, found);
  return found;
}
