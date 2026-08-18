import { DatabaseSync } from "node:sqlite";
import { existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import type { Conflict, Lifecycle, StandSnapshot, Workstream } from "./types";

/** The repository root: the dashboard lives in `site/` inside it. */
export const REPO = path.resolve(process.env.LUMBERJACK_REPO ?? path.join(process.cwd(), ".."));
const STATE = path.join(REPO, ".lumberjack");

const TERMINAL = new Set(["landed", "blocked", "abandoned"]);

type Row = { seq: number; at: string; actor: string; kind: string; payload: string };

export function listStands(): { stand: string; modified: number }[] {
  if (!existsSync(STATE)) return [];
  return readdirSync(STATE)
    .filter((name) => existsSync(path.join(STATE, name, "ledger.db")))
    .map((stand) => ({
      stand,
      modified: statSync(path.join(STATE, stand, "ledger.db")).mtimeMs,
    }))
    .sort((a, b) => b.modified - a.modified);
}

export function latestStand(): string | null {
  return listStands()[0]?.stand ?? null;
}

function readEvents(stand: string): Row[] {
  const file = path.join(STATE, stand, "ledger.db");
  if (!existsSync(file)) return [];
  const db = new DatabaseSync(file, { readOnly: true });
  try {
    return db
      .prepare("SELECT seq, at, actor, kind, payload FROM events ORDER BY seq")
      .all() as unknown as Row[];
  } finally {
    db.close();
  }
}

/**
 * Claude Code stores a session transcript per working directory, under a name
 * derived from the path with every separator and dot flattened to a hyphen.
 */
export function transcriptDir(worktree: string): string {
  return path.join(os.homedir(), ".claude", "projects", worktree.replace(/[/.]/g, "-"));
}

export function transcriptFile(worktree: string): string | null {
  const dir = transcriptDir(worktree);
  if (!existsSync(dir)) return null;
  const files = readdirSync(dir)
    .filter((name) => name.endsWith(".jsonl"))
    .map((name) => ({ name, mtime: statSync(path.join(dir, name)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime);
  return files[0] ? path.join(dir, files[0].name) : null;
}

export function snapshot(stand: string): StandSnapshot | null {
  const rows = readEvents(stand);
  if (rows.length === 0) return null;

  const workstreams = new Map<string, Workstream>();
  const tasks = new Map<string, { state: string; title: string }>();
  const conflicts = new Map<string, Conflict>();
  const leases = new Map<string, { workstream: string; mode: string; scope: string }>();
  const notes: StandSnapshot["notes"] = [];
  const eventCounts: Record<string, number> = {};

  let goal = "";
  let integrationBranch = "";
  let integrationHead: string | null = null;
  let startedAt: number | null = null;
  let halted = false;

  for (const row of rows) {
    eventCounts[row.kind] = (eventCounts[row.kind] ?? 0) + 1;
    const at = Date.parse(row.at);
    const p = JSON.parse(row.payload);

    switch (row.kind) {
      case "stand_started":
        goal = p.goal;
        integrationBranch = p.integration_branch;
        integrationHead = p.base;
        startedAt = at;
        break;
      case "stand_halted":
        halted = true;
        break;
      case "task_planned":
        tasks.set(p.spec.task_id, { state: "pending", title: p.spec.title });
        break;
      case "task_assigned": {
        const w = p.workstream;
        workstreams.set(w.workstream_id, {
          id: w.workstream_id,
          agent: w.agent,
          task: w.task,
          title: tasks.get(w.task)?.title ?? w.task,
          branch: w.worktree.branch,
          worktree: w.worktree.path,
          state: "assigned",
          filesTouched: 0,
          linesChanged: 0,
          claims: 0,
          leases: [],
          conflicts: 0,
          violations: 0,
          lastActivity: at,
          toolCalls: 0,
          present: existsSync(w.worktree.path),
        });
        break;
      }
      case "task_state_changed": {
        tasks.set(p.task_id, {
          state: p.to,
          title: p.state?.spec?.title ?? tasks.get(p.task_id)?.title ?? p.task_id,
        });
        for (const w of workstreams.values()) if (w.task === p.task_id) w.state = p.to;
        break;
      }
      case "claim_requested": {
        const w = workstreams.get(p.claim.workstream);
        if (w) {
          w.claims += 1;
          w.lastActivity = at;
        }
        break;
      }
      case "lease_granted":
        leases.set(p.lease.lease_id, {
          workstream: p.lease.claim.workstream,
          mode: p.lease.claim.mode,
          scope: describeScope(p.lease.claim.scope),
        });
        break;
      case "lease_released":
      case "lease_expired":
        leases.delete(p.lease_id);
        break;
      case "worktree_delta": {
        const w = workstreams.get(p.workstream);
        if (w) {
          w.filesTouched = p.paths.length;
          w.linesChanged = p.lines_changed ?? 0;
          w.lastActivity = at;
        }
        break;
      }
      case "protocol_violation": {
        const w = workstreams.get(p.workstream);
        if (w) w.violations += 1;
        break;
      }
      case "conflict_detected":
        conflicts.set(p.report.conflict_id, {
          id: p.report.conflict_id,
          between: p.report.between,
          source: p.report.source,
          severity: severityLabel(p.report.severity),
          paths: (p.report.files ?? []).map((f: { path: string }) => f.path),
          evidence: p.report.evidence ?? "",
        });
        break;
      case "conflict_cleared":
        conflicts.delete(p.conflict_id);
        break;
      case "note_posted":
        notes.push({ author: p.note.author, topic: p.note.topic, body: p.note.body, at });
        break;
    }
  }

  for (const lease of leases.values()) {
    const w = workstreams.get(lease.workstream);
    if (w) w.leases.push({ mode: lease.mode, scope: lease.scope });
  }
  for (const conflict of conflicts.values()) {
    for (const id of conflict.between) {
      const w = workstreams.get(id);
      if (w) w.conflicts += 1;
    }
  }

  const list = [...workstreams.values()];
  const lifecycle: Lifecycle = halted
    ? "halted"
    : list.length > 0 && list.every((w) => TERMINAL.has(w.state))
      ? "finished"
      : "live";

  return {
    stand,
    goal,
    lifecycle,
    integrationBranch,
    integrationHead,
    startedAt,
    workstreams: list,
    conflicts: [...conflicts.values()],
    notes: notes.slice(-20).reverse(),
    eventCounts,
    totalEvents: rows.length,
  };
}

function describeScope(scope: { kind: string; patterns?: string[]; symbols?: { module: string; qualname: string }[] }): string {
  if (scope.kind === "path") return (scope.patterns ?? []).join(", ");
  return (scope.symbols ?? []).map((s) => `${s.module}:${s.qualname}`).join(", ");
}

function severityLabel(value: number): string {
  if (value >= 30) return "block";
  if (value >= 20) return "warn";
  return "notice";
}
