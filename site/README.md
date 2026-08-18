# Lumberjack dashboard

Real-time local monitoring for a stand: every agent, what it is touching, and its live
session transcript.

```bash
cd site
npm install
npm run dev
```

Then open http://localhost:3000. It finds the most recent stand under `.lumberjack/`
automatically; pass `?stand=<id>` for an older one.

## How it reads the world

Two sources, no daemon and no writes:

- **`.lumberjack/<stand>/ledger.db`** — the append-only event log, read through Node 22's
  built-in `node:sqlite` (so there is no native dependency to build). Every metric on the
  dashboard is a fold over these events, the same way `lj status` computes them.
- **`~/.claude/projects/<worktree>/*.jsonl`** — the Claude Code session transcript for each
  worktree, which the CLI appends to as it works.

Both are polled once a second and pushed over Server-Sent Events. Polling rather than
watching because the writer is a different process and the ledger is a WAL database, where
a filesystem watcher tells you very little.

## Pages

**`/`** — every workstream with live metrics: files touched, lines changed, leases held,
claims made, protocol violations, and time since last activity. Open conflicts and the
blackboard sit below, because those are the things a swarm produces that a single agent
does not.

**`/agents/<workstream>`** — the session transcript, rendered. Tool calls carry an icon and
the file or command they act on; coordination calls (`claim`, `awareness`, `check_merge`,
`request_land`) are tinted so they stand out from ordinary file work. Any code block over
five lines collapses by default — a transcript is mostly file contents, and rendering all
of it turns a log into a haystack.

**`/conflicts/<id>`** — one page per raised conflict, rendered with
[`@pierre/diffs`](https://diffs.com/docs). Each contested file collapses independently:

- Where the two sides genuinely clash, the file is three-way merged with `git merge-file`
  and rendered through `UnresolvedFile`, so the real conflict regions appear with
  take-one-side controls rather than as two diffs you have to reconcile by eye.
- Where they merge cleanly as text — a `blast_radius` or `claim_overlap` conflict, where
  the danger is semantic — each side is shown as a split diff against the base they
  diverged from, which is exactly what git merged.

**Token hover** works across both. Point at any name and its definition appears: the
signature, the kind, the line, and the file it comes from. Definitions are indexed
repo-wide from one `git grep`, because the names you most want explained are the ones a
file *imports*, and those are never defined in front of you.

## Configuration

`LUMBERJACK_REPO` overrides the repository root, which otherwise defaults to the parent of
`site/`.
