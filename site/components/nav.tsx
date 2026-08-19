"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  ChevronRight,
  FolderGit2,
  Layers,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings,
  X,
} from "lucide-react";
import { AddRepo } from "@/components/add-repo";
import { notifyReposChanged, useReposChanged } from "@/lib/repos-changed";
import { NewRun } from "@/components/new-run";
import { SettingsModal } from "@/components/settings-modal";
import { Button } from "@/components/ui/button";
import { LIFECYCLE_TONE, StatusDot } from "@/components/ui/status";
import { cn } from "@/lib/utils";
import type { Lifecycle } from "@/lib/types";

type Repo = { path: string; name: string; addedAt: number };
type StandEntry = {
  stand: string;
  goal: string;
  title: string;
  lifecycle: Lifecycle;
  workstreams: number;
  conflicts: number;
  comments: number;
};

const COLLAPSED_KEY = "lj.nav.collapsed";

/** A nav row: one height, one radius, one hover. Selection is a wash, never a border. */
const ROW = "flex items-center gap-2 rounded-md px-2 py-1 text-sm transition-colors";
const SELECTED = "bg-accent text-foreground";
const RESTING = "text-muted-foreground hover:bg-accent/50 hover:text-foreground";

export function Nav() {
  const params = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const activeRepo = params.get("repo");
  const activeStand = params.get("stand");

  const [collapsed, setCollapsed] = useState(false);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [stands, setStands] = useState<Record<string, StandEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  const [newRunFor, setNewRunFor] = useState<Repo | null>(null);
  const [removing, setRemoving] = useState<Repo | null>(null);
  const [settings, setSettings] = useState(false);

  /**
   * Removing a project unregisters it and touches nothing on disk.
   *
   * The runs, worktrees and branches are the repository's, not the dashboard's, and a
   * control in a sidebar is the wrong place to destroy work. Deleting a run has its own
   * confirmation on the run itself, where what would be lost can be counted.
   */
  const forget = async (repo: Repo) => {
    await fetch("/api/repos", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: repo.path }),
    });
    setRemoving(null);
    // The nav knowing is not enough: the page beside it is server-rendered, so the file
    // tree of a project just removed stays on screen until the route is re-run.
    router.replace("/");
    notifyReposChanged();
  };

  useEffect(() => {
    setCollapsed(localStorage.getItem(COLLAPSED_KEY) === "1");
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((value) => {
      localStorage.setItem(COLLAPSED_KEY, value ? "0" : "1");
      return !value;
    });
  };

  const loadRepos = useCallback(async () => {
    const response = await fetch("/api/repos");
    const data = (await response.json()) as { repos: Repo[] };
    setRepos(data.repos);
    setLoaded(true);
    return data.repos;
  }, []);

  const loadStands = useCallback(async (repo: string) => {
    const response = await fetch(`/api/stands?repo=${encodeURIComponent(repo)}`);
    const data = (await response.json()) as { stands: StandEntry[] };
    setStands((current) => ({ ...current, [repo]: data.stands }));
  }, []);

  useEffect(() => {
    void loadRepos().then((found) => {
      // Open the repository being looked at, so the runs under it are one glance away.
      const current = activeRepo ?? found[0]?.path;
      if (current) {
        setExpanded(new Set([current]));
        void loadStands(current);
      }
    });
  }, [loadRepos, loadStands, activeRepo]);

  useEffect(() => {
    const timer = setInterval(() => {
      for (const repo of expanded) void loadStands(repo);
    }, 3000);
    return () => clearInterval(timer);
  }, [expanded, loadStands]);

  // Any project added or removed anywhere, by any control, lands here.
  useReposChanged(
    useCallback(() => {
      void loadRepos();
      router.refresh();
    }, [loadRepos, router]),
  );

  const toggleRepo = (repo: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(repo)) next.delete(repo);
      else {
        next.add(repo);
        void loadStands(repo);
      }
      return next;
    });
  };

  const repoHref = (repo: Repo) =>
    repo.addedAt === 0 ? "/" : `/?repo=${encodeURIComponent(repo.path)}`;
  const standHref = (repo: Repo, stand: string) =>
    repo.addedAt === 0
      ? `/?stand=${stand}`
      : `/?repo=${encodeURIComponent(repo.path)}&stand=${stand}`;

  if (collapsed) {
    return (
      <nav className="flex w-12 shrink-0 flex-col items-center gap-1 border-r border-border py-2.5">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="expand navigation"
          onClick={toggleCollapsed}
          className="text-muted-foreground"
        >
          <PanelLeftOpen />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="add a project"
          onClick={() => setAdding(true)}
          className="text-muted-foreground"
        >
          <Plus />
        </Button>
        <div className="mt-1 flex flex-col gap-0.5">
          {repos.map((repo) => (
            <Link
              key={repo.path}
              href={repoHref(repo)}
              title={repo.name}
              className={cn(
                "flex h-7 w-7 items-center justify-center rounded-md transition-colors",
                repo.path === (activeRepo ?? repos[0]?.path) ? SELECTED : RESTING,
              )}
            >
              <FolderGit2 className="h-3.5 w-3.5" />
            </Link>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setSettings(true)}
          aria-label="settings"
          title="Settings"
          className="mt-auto rounded-md p-1.5 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
        >
          <Settings className="h-4 w-4" />
        </button>
        {adding && <AddRepo onClose={() => setAdding(false)} onAdded={() => undefined} />}
        {settings && <SettingsModal onClose={() => setSettings(false)} />}
      </nav>
    );
  }

  return (
    <nav className="flex w-60 shrink-0 flex-col border-r border-border">
      <div className="flex items-center gap-1 px-2 py-2.5">
        <Button
          variant="outline"
          size="sm"
          onClick={() => setAdding(true)}
          className="flex-1 justify-start text-muted-foreground"
        >
          <Plus />
          Add project
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="collapse navigation"
          onClick={toggleCollapsed}
          className="text-muted-foreground"
        >
          <PanelLeftClose />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {loaded && repos.length === 0 && (
          <p className="px-2 py-6 text-xs leading-relaxed text-muted-foreground">
            No projects. Use <span className="text-foreground">Add project</span> to point the
            dashboard at a git repository.
          </p>
        )}
        {repos.map((repo) => {
          const isOpen = expanded.has(repo.path);
          const runs = stands[repo.path] ?? [];
          const current = repo.path === (activeRepo ?? repos[0]?.path);
          return (
            <div key={repo.path} className="mb-0.5">
              <div className="group/row flex items-center">
                <button
                  type="button"
                  onClick={() => toggleRepo(repo.path)}
                  aria-label={isOpen ? "collapse" : "expand"}
                  className="p-0.5 text-muted-foreground transition-colors hover:text-foreground"
                >
                  <ChevronRight
                    className={cn("h-3 w-3 transition-transform", isOpen && "rotate-90")}
                  />
                </button>
                <Link
                  href={repoHref(repo)}
                  className={cn(
                    ROW,
                    "min-w-0 flex-1",
                    current && !activeStand && pathname === "/" ? SELECTED : RESTING,
                  )}
                >
                  <FolderGit2 className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">{repo.name}</span>
                </Link>
                {/* Row actions stay out of the way until the row is under the cursor. */}
                <span className="flex shrink-0 opacity-0 transition-opacity group-hover/row:opacity-100 focus-within:opacity-100">
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    title={`new run in ${repo.name}`}
                    aria-label={`new run in ${repo.name}`}
                    onClick={() => setNewRunFor(repo)}
                    className="text-muted-foreground"
                  >
                    <Plus />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    title={`remove ${repo.name} from the dashboard`}
                    aria-label={`remove ${repo.name} from the dashboard`}
                    onClick={() => setRemoving(repo)}
                    className="text-muted-foreground hover:text-destructive"
                  >
                    <X />
                  </Button>
                </span>
              </div>

              {removing?.path === repo.path && (
                <div className="mt-1 ml-4 rounded-md border border-border bg-card p-2.5">
                  <p className="text-xs font-medium">Remove {repo.name}?</p>
                  <p className="mt-1 text-2xs leading-relaxed text-muted-foreground">
                    Takes it off this dashboard. Nothing on disk is deleted -- its runs,
                    worktrees and branches stay where they are, and you can add it again.
                  </p>
                  <div className="mt-2 flex gap-1.5">
                    <Button variant="destructive" size="xs" onClick={() => void forget(repo)}>
                      Remove
                    </Button>
                    <Button variant="ghost" size="xs" onClick={() => setRemoving(null)}>
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              {isOpen && (
                <div className="mt-0.5 ml-3.5 border-l border-border pl-1.5">
                  {runs.length === 0 && (
                    <p className="px-2 py-1 font-mono text-2xs text-muted-foreground/60">
                      no runs yet
                    </p>
                  )}
                  {runs.map((run) => (
                    <Link
                      key={run.stand}
                      href={standHref(repo, run.stand)}
                      title={run.title || run.goal || run.stand}
                      className={cn(ROW, activeStand === run.stand ? SELECTED : RESTING)}
                    >
                      <StatusDot tone={LIFECYCLE_TONE[run.lifecycle]} />
                      <span className="truncate font-mono text-xs">
                        {run.title || run.goal || run.stand}
                      </span>
                      {run.comments > 0 && (
                        <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">
                          {run.comments}
                        </span>
                      )}
                    </Link>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <footer className="flex items-center gap-1.5 border-t border-border py-2 pl-3 pr-2">
        <span className="flex items-center gap-1.5 font-mono text-2xs text-muted-foreground/60">
          <Layers className="h-3 w-3" />
          lumberjack
        </span>
        <button
          type="button"
          onClick={() => setSettings(true)}
          aria-label="settings"
          title="Settings"
          className="ml-auto rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
        >
          <Settings className="h-3.5 w-3.5" />
        </button>
      </footer>

      {adding && <AddRepo onClose={() => setAdding(false)} onAdded={() => undefined} />}
      {newRunFor && (
        <NewRun
          repo={newRunFor.path}
          repoName={newRunFor.name}
          onClose={() => setNewRunFor(null)}
          onStarted={() => {
            setExpanded((current) => new Set(current).add(newRunFor.path));
            void loadStands(newRunFor.path);
          }}
        />
      )}
      {settings && <SettingsModal onClose={() => setSettings(false)} />}
    </nav>
  );
}
