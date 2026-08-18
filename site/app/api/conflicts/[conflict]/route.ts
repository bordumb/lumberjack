import { latestStand, snapshot } from "@/lib/ledger";
import {
  mergeBase,
  mergeWithMarkers,
  repoDefinitions,
  resolveRef,
  showFile,
} from "@/lib/git";
import { extractSymbols } from "@/lib/symbols";

export const dynamic = "force-dynamic";

/**
 * A conflict is between two workstreams, so the honest view is each side against the
 * base they diverged from -- which is exactly what git merges, and therefore exactly
 * what produced the conflict.
 */
export async function GET(req: Request, ctx: { params: Promise<{ conflict: string }> }) {
  const { conflict: id } = await ctx.params;
  const stand = new URL(req.url).searchParams.get("stand") ?? latestStand();
  const state = stand ? snapshot(stand) : null;
  const conflict = state?.conflicts.find((item) => item.id === id);
  if (!state || !conflict) return new Response("unknown conflict", { status: 404 });

  const [left, right] = conflict.between.map((workstream) =>
    state.workstreams.find((item) => item.id === workstream),
  );

  const leftRef = left ? ((await resolveRef(left.branch)) ?? left.branch) : null;
  const rightRef = right ? ((await resolveRef(right.branch)) ?? right.branch) : null;
  const base =
    leftRef && rightRef
      ? ((await mergeBase(leftRef, rightRef)) ?? state.integrationHead)
      : state.integrationHead;

  // Repo-wide definitions first, so an imported name still resolves; the file's own
  // parse layers over the top because it can follow a signature across lines.
  const repo = base ? await repoDefinitions(base) : {};
  const ambient: Record<string, { name: string; kind: string; signature: string; line: number }> =
    Object.fromEntries(
      Object.entries(repo).map(([name, entry]) => [
        name,
        {
          name,
          kind: entry.text.startsWith("class") ? "class" : "function",
          signature: `${entry.text.replace(/:$/, "")}   ·   ${entry.file}`,
          line: entry.line,
        },
      ]),
    );

  const files = await Promise.all(
    conflict.paths.map(async (path) => {
      const [baseText, oursText, theirsText] = await Promise.all([
        base ? showFile(base, path) : null,
        leftRef ? showFile(leftRef, path) : null,
        rightRef ? showFile(rightRef, path) : null,
      ]);
      const { merged, conflicted } = await mergeWithMarkers(
        baseText ?? "",
        oursText ?? "",
        theirsText ?? "",
        { ours: left?.title ?? "ours", theirs: right?.title ?? "theirs" },
      );
      return {
        path,
        // Hover reads from both sides: a symbol one agent added is still worth
        // explaining when you are looking at the other agent's version.
        symbols: {
          ...ambient,
          ...extractSymbols(baseText ?? "", path),
          ...extractSymbols(theirsText ?? "", path),
          ...extractSymbols(oursText ?? "", path),
        },
        base: baseText ?? "",
        ours: oursText ?? "",
        theirs: theirsText ?? "",
        merged,
        conflicted,
        oursChanged: (oursText ?? "") !== (baseText ?? ""),
        theirsChanged: (theirsText ?? "") !== (baseText ?? ""),
      };
    }),
  );

  return Response.json({
    conflict,
    stand: state.stand,
    base,
    left: left ? { id: left.id, agent: left.agent, title: left.title, branch: left.branch } : null,
    right: right
      ? { id: right.id, agent: right.agent, title: right.title, branch: right.branch }
      : null,
    files,
  });
}
