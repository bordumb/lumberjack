import type { Conflict, ReviewComment, StandSnapshot, Workstream } from "./types";

/**
 * One task, woven from every lane that ever served it.
 *
 * A workstream is how a task was worked on during one session; a task is the thing
 * itself. Pausing and continuing a run gives a task a second lane, and every signal in
 * the system -- deltas, leases, conflicts, transcripts -- is keyed on the lane. Read a
 * single lane and a task that has changed 25 files reports 7.
 *
 * So this is the one place lanes are combined, and every surface reads tasks rather
 * than workstreams. Anything keyed on a lane that is not woven here will silently show
 * a fraction of the truth, which is the failure this type exists to make impossible.
 */
export type Lane = {
  id: string;
  agent: string;
  branch: string;
  worktree: string;
  active: boolean;
  present: boolean;
  filesTouched: number;
  linesChanged: number;
};

export type TaskView = {
  task: string;
  title: string;
  state: string;
  /** Newest first: the lane doing the work now leads. */
  lanes: Lane[];
  agents: string[];
  sessions: number;
  files: string[];
  linesChanged: number;
  leases: { mode: string; scope: string }[];
  claims: number;
  violations: number;
  conflicts: Conflict[];
  comments: ReviewComment[];
  lastActivity: number | null;
  branch: string;
  /** The lane a reader should be pointed at: the live one if there is one. */
  primary: Lane;
};

function laneOf(workstream: Workstream): Lane {
  return {
    id: workstream.id,
    agent: workstream.agent,
    branch: workstream.branch,
    worktree: workstream.worktree,
    active: workstream.active,
    present: workstream.present,
    filesTouched: workstream.filesTouched,
    linesChanged: workstream.linesChanged,
  };
}

export function weave(state: StandSnapshot): TaskView[] {
  const byTask = new Map<string, Workstream[]>();
  for (const workstream of state.workstreams) {
    byTask.set(workstream.task, [...(byTask.get(workstream.task) ?? []), workstream]);
  }

  const views: TaskView[] = [];
  for (const [task, all] of byTask) {
    const ordered = [...all].sort(
      (a, b) => Number(b.active) - Number(a.active) || (b.lastActivity ?? 0) - (a.lastActivity ?? 0),
    );
    const lanes = ordered.map(laneOf);
    const ids = new Set(ordered.map((item) => item.id));
    const agents = [...new Set(ordered.map((item) => item.agent))];

    views.push({
      task,
      title: ordered[0].title,
      state: ordered[0].state,
      lanes,
      agents,
      sessions: lanes.length,
      // Files are a set: two lanes editing the same file is one file touched.
      files: [...new Set(ordered.flatMap((item) => item.touched ?? []))],
      // Lines are a sum: the lanes ran one after another on one branch, so their
      // changes stack rather than overlap.
      linesChanged: ordered.reduce((total, item) => total + item.linesChanged, 0),
      leases: ordered.flatMap((item) => item.leases),
      claims: ordered.reduce((total, item) => total + item.claims, 0),
      violations: ordered.reduce((total, item) => total + item.violations, 0),
      conflicts: state.conflicts.filter((item) => item.between.some((side) => ids.has(side))),
      comments: state.comments.filter(
        (item) =>
          (item.workstream && ids.has(item.workstream)) ||
          (item.conflictId &&
            state.conflicts.some(
              (conflict) =>
                conflict.id === item.conflictId && conflict.between.some((side) => ids.has(side)),
            )),
      ),
      lastActivity: Math.max(...ordered.map((item) => item.lastActivity ?? 0)) || null,
      branch: ordered[0].branch,
      primary: lanes[0],
    });
  }

  return views.sort((a, b) => a.title.localeCompare(b.title));
}

/** The task a lane belongs to, so a link to any lane resolves to the whole task. */
export function taskOfLane(state: StandSnapshot, lane: string): TaskView | undefined {
  return weave(state).find((view) => view.lanes.some((item) => item.id === lane));
}
