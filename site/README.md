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

## Projects and runs

The left nav is the hierarchy: a repository, with its runs nested beneath it. Clicking a
repository opens its file tree; clicking a run opens that run's dashboard. It collapses
to an icon rail, and remembers which you chose.

**Add project** at the top opens a folder browser. It browses the *server's* filesystem
rather than using the browser's directory picker, because both `showDirectoryPicker()`
and `<input webkitdirectory>` deliberately withhold the absolute path and git needs one.
The dashboard already runs on the same machine as the repositories it reads, so this is
the trust boundary a terminal has, not a wider one. Folders containing a `.git` are
marked, and only those can be added.

Registered repositories live in `~/.lumberjack/repos.json`. That file is the only stored
state in the dashboard, and deliberately so: it holds paths a person chose, which is the
one thing here that cannot be recomputed from git or from a ledger.

## How it reads the world

Two sources, no daemon and no writes:

- **the working tree on disk**, walked directly rather than read from `git ls-files`.
  Ignored paths are included and marked ignored, not omitted: `.venv` and `.lumberjack`
  are gitignored by definition, and `.lumberjack` is where the runs live, so a tree that
  hides them is useless for inspecting one by hand. Only `.git` and `node_modules` are
  skipped — object storage and 30,000 files nobody browses.
- **`<repo>/.lumberjack/<stand>/ledger.db`** — the append-only event log, read through Node 22's
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
`request_land`) are tinted so they stand out from ordinary file work.

Every snippet goes through `@pierre/diffs`, so the log is syntax-highlighted rather than
grey text. An `Edit` shows as a real split diff, because the replacement text alone does
not tell you what changed. Tool results are highlighted in the language of the call they
answer — a `Read` of a `.py` file reads as Python — and the Read tool's own line-number
gutter is stripped first so it does not collide with the one the renderer draws. Anything
over five lines collapses by default, since a transcript is mostly file contents.

Token hover works here too, from the same repo-wide index the conflict pages use.

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

## Leaving review comments

Hover a line and a blue `+` appears in the gutter; click it and the composer opens
inline at that line. Dragging across the gutter comments on a range. The comment
is not a note on a screen:

- it is **addressed** — on an agent page to that agent, on a conflict page to both
  participants, because a conflict is a dispute and telling one side is telling nobody;
- it **leads the agent's awareness digest**, above peers and open conflicts;
- it **blocks that work from landing** until it is resolved, which the merge train
  enforces rather than the UI.

The status on each comment is read from what actually happened -- `queued` until the
agent reads its inbox, `delivered` once it has, `addressed` when its next delta touches
the file, `resolved` when you close it. None of it is assumed.

Posting shells out to `lj comment` rather than writing the ledger from the dashboard:
the event shape and the routing rule live in the Python domain, and a second copy here
would drift. The same commands work from a terminal:

```bash
uv run lj comment "this drops the error" --file pkg/core.py --line 12 --workstream ws-abc
uv run lj comments
uv run lj resolve comment-abc123
```

## Configuration

`LUMBERJACK_REPO` overrides the repository root, which otherwise defaults to the parent of
`site/`.
