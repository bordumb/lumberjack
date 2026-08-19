export type Lifecycle = "live" | "stale" | "halted" | "finished";

export type Workstream = {
  id: string;
  agent: string;
  task: string;
  title: string;
  branch: string;
  worktree: string;
  state: string;
  filesTouched: number;
  linesChanged: number;
  claims: number;
  leases: { mode: string; scope: string }[];
  conflicts: number;
  violations: number;
  lastActivity: number | null;
  toolCalls: number;
  present: boolean;
};

export type Conflict = {
  id: string;
  between: [string, string];
  source: string;
  severity: string;
  paths: string[];
  evidence: string;
};

export type CommentStatus = "queued" | "delivered" | "addressed" | "resolved";

export type ReviewComment = {
  id: string;
  author: string;
  body: string;
  file: string;
  lineStart: number;
  lineEnd: number;
  side: string;
  workstream: string | null;
  conflictId: string | null;
  postedAt: number;
  resolved: boolean;
  notified: string[];
  status: CommentStatus;
  replies: { frm: string; body: string; at: number }[];
};

export type StandSnapshot = {
  stand: string;
  goal: string;
  name: string;
  title: string;
  pid: number | null;
  resumedFrom: string | null;
  lifecycle: Lifecycle;
  integrationBranch: string;
  integrationHead: string | null;
  startedAt: number | null;
  workstreams: Workstream[];
  conflicts: Conflict[];
  notes: { author: string; topic: string; body: string; at: number }[];
  comments: ReviewComment[];
  eventCounts: Record<string, number>;
  totalEvents: number;
};

export type LogEntry = {
  seq: number;
  role: "assistant" | "user" | "system" | "result";
  at: number | null;
  text?: string;
  tool?: {
    name: string;
    label: string;
    target: string;
    body: string | null;
    before?: string | null;
    language: string;
  };
  result?: { ok: boolean; preview: string; lines: number };
};
