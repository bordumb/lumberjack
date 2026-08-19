import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { DEFAULT_REPO } from "@/lib/repos";

const run = promisify(execFile);

export const dynamic = "force-dynamic";

/**
 * The picker reads the same list the runner honours.
 *
 * Hardcoding models here would let the UI offer one the runner rejects, which is a
 * trap rather than a convenience, so this asks `lj` what it accepts.
 */
export async function GET() {
  try {
    const { stdout } = await run("uv", ["run", "lj", "models", "--as-json"], {
      cwd: DEFAULT_REPO,
    });
    return Response.json({ models: JSON.parse(stdout) });
  } catch (cause) {
    return Response.json(
      { models: [], error: String((cause as { stderr?: string }).stderr ?? cause).slice(-400) },
      { status: 500 },
    );
  }
}
