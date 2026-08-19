"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import {
  ChevronRight,
  FolderGit2,
  Layers,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
} from "lucide-react";
import { AddRepo } from "@/components/add-repo";
import { cn } from "@/lib/utils";

type Repo = { path: string; name: string; addedAt: number };
type StandEntry = {
  stand: string;
  goal: string;
  lifecycle: "live" | "halted" | "finished";
  workstreams: number;
  conflicts: number;
  comments: number;
};

const DOT: Record<StandEntry["lifecycle"], string> = {
  live: "bg-emerald-500",
  halted: "bg-destructive",
  finished: "bg-amber-500/70",
};

const COLLAPSED_KEY = "lj.nav.collapsed";

export function Nav() {
  const params = useSearchParams();
  const pathname = usePathname();
  const activeRepo = params.get("repo");
  const activeStand = params.get("stand");

  const [collapsed, setCollapsed] = useState(false);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [stands, setStands] = useState<Record<string, StandEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);

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
      <nav className="flex w-12 shrink-0 flex-col items-center gap-2 border-r border-border/60 py-3">
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-label="expand navigation"
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => setAdding(true)}
          aria-label="add a project"
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
        >
          <Plus className="h-4 w-4" />
        </button>
        <div className="mt-1 flex flex-col gap-1.5">
          {repos.map((repo) => (
            <Link
              key={repo.path}
              href={repoHref(repo)}
              title={repo.name}
              className={cn(
                "rounded-md p-1.5 text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                repo.path === (activeRepo ?? repos[0]?.path) && "bg-primary/10 text-primary",
              )}
            >
              <FolderGit2 className="h-4 w-4" />
            </Link>
          ))}
        </div>
        {adding && (
          <AddRepo onClose={() => setAdding(false)} onAdded={() => void loadRepos()} />
        )}
      </nav>
    );
  }

  return (
    <nav className="flex w-64 shrink-0 flex-col border-r border-border/60">
      <div className="flex items-center gap-1 px-3 py-3">
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="flex flex-1 items-center gap-1.5 rounded-md border border-border px-2 py-1.5 text-[12px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-primary"
        >
          <Plus className="h-3.5 w-3.5" />
          Add project
        </button>
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-label="collapse navigation"
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
        >
          <PanelLeftClose className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {repos.map((repo) => {
          const isOpen = expanded.has(repo.path);
          const runs = stands[repo.path] ?? [];
          const current = repo.path === (activeRepo ?? repos[0]?.path);
          return (
            <div key={repo.path} className="mb-1">
              <div className="flex items-center gap-0.5">
                <button
                  type="button"
                  onClick={() => toggleRepo(repo.path)}
                  aria-label={isOpen ? "collapse" : "expand"}
                  className="rounded p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <ChevronRight
                    className={cn("h-3.5 w-3.5 transition-transform", isOpen && "rotate-90")}
                  />
                </button>
                <Link
                  href={repoHref(repo)}
                  className={cn(
                    "flex flex-1 items-center gap-1.5 rounded-md px-1.5 py-1 text-[13px] transition-colors hover:bg-muted/50",
                    current && !activeStand && pathname === "/"
                      ? "bg-primary/10 text-primary"
                      : "text-foreground/90",
                  )}
                >
                  <FolderGit2 className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">{repo.name}</span>
                </Link>
              </div>

              {isOpen && (
                <div className="ml-4 mt-0.5 border-l border-border/50 pl-2">
                  {runs.length === 0 && (
                    <p className="px-1.5 py-1 font-mono text-[10.5px] text-muted-foreground/60">
                      no runs yet
                    </p>
                  )}
                  {runs.map((run) => (
                    <Link
                      key={run.stand}
                      href={standHref(repo, run.stand)}
                      title={run.goal || run.stand}
                      className={cn(
                        "flex items-center gap-1.5 rounded-md px-1.5 py-1 transition-colors hover:bg-muted/50",
                        activeStand === run.stand
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground",
                      )}
                    >
                      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", DOT[run.lifecycle])} />
                      <span className="truncate font-mono text-[11px]">
                        {run.goal || run.stand}
                      </span>
                      {run.comments > 0 && (
                        <span className="ml-auto shrink-0 rounded-full bg-primary/15 px-1 font-mono text-[9.5px] text-primary">
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

      <footer className="border-t border-border/60 px-3 py-2">
        <span className="flex items-center gap-1.5 font-mono text-[10.5px] text-muted-foreground/60">
          <Layers className="h-3 w-3" />
          lumberjack
        </span>
      </footer>

      {adding && <AddRepo onClose={() => setAdding(false)} onAdded={() => void loadRepos()} />}
    </nav>
  );
}
