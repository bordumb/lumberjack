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

## Configuration

`LUMBERJACK_REPO` overrides the repository root, which otherwise defaults to the parent of
`site/`.
