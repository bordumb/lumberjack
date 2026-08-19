import { DatabaseSync } from "node:sqlite";
import { existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { DEFAULT_REPO } from "./repos";
import os from "node:os";
import type {
  CommentStatus,
  Conflict,
  Lifecycle,
  ReviewComment,
  StandSnapshot,
  Workstream,
} from "./types";

/** The repository root: the dashboard lives in `site/` inside it. */
export { DEFAULT_REPO as REPO } from "./repos";

const stateRoot = (repo: string): string => path.join(repo, ".lumberjack");

const TERMINAL = new Set(["landed", "blocked", "abandoned"]);

type Row = { seq: number; at: string; actor: string; kind: string; payload: string };

export function listStands(repo: string = DEFAULT_REPO): { stand: string; modified: number }[] {
  const state = stateRoot(repo);
  if (!existsSync(state)) return [];
  return readdirSync(state)
    .filter((name) => existsSync(path.join(state, name, "ledger.db")))
    .map((stand) => ({
      stand,
      modified: statSync(path.join(state, stand, "ledger.db")).mtimeMs,
    }))
    .sort((a, b) => b.modified - a.modified);
}

export function latestStand(repo: string = DEFAULT_REPO): string | null {
  return listStands(repo)[0]?.stand ?? null;
}

/**
 * Snapshots are cached on the ledger's size and mtime.
 *
 * The nav polls every stand every few seconds and a snapshot is a fold over the whole
 * log -- one of these is already 5 MB. Re-reading them on a timer pinned a core and was
 * slow enough to distort the Python test suite running alongside it. An append-only log
 * makes the invalidation trivial: if the file has not changed, neither has the answer.
 */
const snapshots = new Map<string, { key: string; value: StandSnapshot | null }>();

function ledgerKey(file: string): string {
  try {
    const stat = statSync(file);
    return `${stat.size}:${stat.mtimeMs}`;
  } catch {
    return "missing";
  }
}

function readEvents(stand: string, repo: string): Row[] {
  const file = path.join(stateRoot(repo), stand, "ledger.db");
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

export function snapshot(stand: string, repo: string = DEFAULT_REPO): StandSnapshot | null {
  const file = path.join(stateRoot(repo), stand, "ledger.db");
  const key = ledgerKey(file);
  const cached = snapshots.get(`${repo}:${stand}`);
  if (cached && cached.key === key) return cached.value;

  const value = fold(stand, repo);
  snapshots.set(`${repo}:${stand}`, { key, value });
  return value;
}

function fold(stand: string, repo: string): StandSnapshot | null {
  const rows = readEvents(stand, repo);
  if (rows.length === 0) return null;

  const workstreams = new Map<string, Workstream>();
  const tasks = new Map<string, { state: string; title: string }>();
  const conflicts = new Map<string, Conflict>();
  const leases = new Map<string, { workstream: string; mode: string; scope: string }>();
  const notes: StandSnapshot["notes"] = [];
  const comments = new Map<string, ReviewComment>();
  const awarenessAt = new Map<string, number>();
  const eventCounts: Record<string, number> = {};

  let goal = "";
  let name = "";
  let pid: number | null = null;
  let resumedFrom: string | null = null;
  let session = 1;
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
        pid = p.pid ?? null;
        resumedFrom = p.resumed_from ?? null;
        integrationBranch = p.integration_branch;
        integrationHead = p.base;
        startedAt = at;
        break;
      case "stand_halted":
        halted = true;
        break;
      case "stand_resumed":
        // A stand can be picked up again: the halt is lifted and a new supervisor
        // owns it. Without this the badge stays halted through a working session.
        halted = false;
        pid = p.pid ?? null;
        session = p.session ?? session;
        break;
      case "stand_renamed":
        name = p.name;
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
      case "review_comment_posted": {
        const c = p.comment;
        comments.set(c.comment_id, {
          id: c.comment_id,
          author: c.author,
          body: c.body,
          file: c.file,
          lineStart: c.line_start,
          lineEnd: c.line_end,
          side: c.side,
          workstream: c.workstream ?? null,
          conflictId: c.conflict_id ?? null,
          postedAt: Date.parse(c.posted_at),
          resolved: false,
          notified: p.notified ?? [],
          status: "queued",
          replies: [],
        });
        break;
      }
      case "review_comment_resolved": {
        const existing = comments.get(p.comment_id);
        if (existing) comments.set(p.comment_id, { ...existing, resolved: true });
        break;
      }
      case "message_sent": {
        // An agent answering a review comment is a reply, not chatter.
        const message = p.message;
        for (const comment of comments.values()) {
          if (
            comment.notified.includes(message.frm) &&
            at >= comment.postedAt &&
            message.subject.includes(comment.file)
          ) {
            comment.replies.push({ frm: message.frm, body: message.body, at });
          }
        }
        break;
      }
      case "message_read":
        awarenessAt.set(String(row.actor), at);
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

  // The status ladder is read from what happened, never assumed: an agent has seen a
  // comment when it actually read its inbox, and has addressed it when its next delta
  // touched the file.
  for (const comment of comments.values()) {
    comment.status = statusOf(comment, workstreams, awarenessAt);
  }

  const list = [...workstreams.values()];
  // "live" has to mean a supervisor is actually running. A crashed stand is
  // un-halted with work outstanding, which is indistinguishable from a working one
  // unless liveness is checked rather than assumed.
  const lifecycle: Lifecycle = halted
    ? "halted"
    : list.length > 0 && list.every((w) => TERMINAL.has(w.state))
      ? "finished"
      : alive(pid, path.join(stateRoot(repo), stand, "ledger.db"))
        ? "live"
        : "stale";

  return {
    stand,
    goal,
    name,
    title: name || goal || stand,
    pid,
    session,
    resumedFrom,
    lifecycle,
    integrationBranch,
    integrationHead,
    startedAt,
    workstreams: list,
    conflicts: [...conflicts.values()],
    notes: notes.slice(-20).reverse(),
    comments: [...comments.values()],
    eventCounts,
    totalEvents: rows.length,
  };
}

const IDLE_MS = 3 * 60 * 1000;

/** The process check when there is a pid; otherwise whether the log is still moving. */
function alive(pid: number | null, ledger: string): boolean {
  if (pid !== null) {
    try {
      process.kill(pid, 0);
      return true;
    } catch (cause) {
      return (cause as NodeJS.ErrnoException).code === "EPERM";
    }
  }
  try {
    return Date.now() - statSync(ledger).mtimeMs < IDLE_MS;
  } catch {
    return false;
  }
}

function statusOf(
  comment: ReviewComment,
  workstreams: Map<string, Workstream>,
  awarenessAt: Map<string, number>,
): CommentStatus {
  if (comment.resolved) return "resolved";
  const owner = comment.workstream ? workstreams.get(comment.workstream) : undefined;
  const touchedSince =
    owner && (owner.lastActivity ?? 0) > comment.postedAt && owner.filesTouched > 0;
  if (touchedSince) return "addressed";
  const seen = comment.notified.some(
    (agent) => (awarenessAt.get(agent) ?? 0) >= comment.postedAt,
  );
  return seen ? "delivered" : "queued";
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
