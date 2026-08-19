import { latestStand, snapshot } from "@/lib/ledger";
import { repoDefinitions } from "@/lib/git";

export const dynamic = "force-dynamic";

/** Repo-wide definitions, so token hover works in the agent log too. */
export async function GET(req: Request) {
  const stand = new URL(req.url).searchParams.get("stand") ?? latestStand();
  const state = stand ? snapshot(stand) : null;
  const rev = state?.integrationHead ?? "HEAD";
  const found = await repoDefinitions(rev);

  return Response.json(
    Object.fromEntries(
      Object.entries(found).map(([name, entry]) => [
        name,
        {
          name,
          kind: entry.text.startsWith("class") ? "class" : "function",
          signature: `${entry.text.replace(/:$/, "")}   ·   ${entry.file}`,
          line: entry.line,
        },
      ]),
    ),
  );
}
