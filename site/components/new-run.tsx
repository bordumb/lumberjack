"use client";

import { useEffect, useState } from "react";
import { FileText, Play, Plus, Trash2, X } from "lucide-react";
import { cn } from "@/lib/utils";

type ModelChoice = {
  provider: string;
  id: string;
  label: string;
  note: string;
  default: boolean;
};

type Draft = {
  key: number;
  title: string;
  intent: string;
  provider: string;
  model: string;
  spec: string | null;
};

const PROMPT_PLACEHOLDER =
  "Enter full prompt or point your agent/worktree at a pre-written spec (recommended).";

let nextKey = 1;

const blank = (model: string): Draft => ({
  key: nextKey++,
  title: "",
  intent: "",
  provider: "anthropic",
  model,
  spec: null,
});

export function NewRun({
  repo,
  repoName,
  onClose,
  onStarted,
}: {
  repo: string;
  repoName: string;
  onClose: () => void;
  onStarted: () => void;
}) {
  const [name, setName] = useState("");
  const [models, setModels] = useState<ModelChoice[]>([]);
  const [agents, setAgents] = useState<Draft[]>([]);
  const [specs, setSpecs] = useState<string[]>([]);
  const [runtime, setRuntime] = useState("claude_code");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void fetch("/api/models")
      .then((response) => response.json() as Promise<{ models: ModelChoice[] }>)
      .then((data) => {
        setModels(data.models);
        const fallback = data.models.find((item) => item.default)?.id ?? "claude-opus-5";
        setAgents([blank(fallback)]);
      });
    void fetch(`/api/repo/tree?repo=${encodeURIComponent(repo)}`)
      .then((response) => response.json() as Promise<{ paths: string[] }>)
      .then((data) =>
        setSpecs(
          data.paths.filter(
            (item) => /(^|\/)(docs\/)?specs?\//.test(item) && item.endsWith(".md"),
          ),
        ),
      )
      .catch(() => setSpecs([]));
  }, [repo]);

  const update = (key: number, patch: Partial<Draft>) =>
    setAgents((current) =>
      current.map((agent) => (agent.key === key ? { ...agent, ...patch } : agent)),
    );

  const start = async () => {
    setBusy(true);
    setError(null);
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo,
        name,
        runtime,
        agents: agents.map((agent, index) => ({
          title: agent.title || agent.spec?.split("/").pop()?.replace(/\.md$/, "") || `agent ${index + 1}`,
          intent: agent.spec
            ? `Implement the specification in \`${agent.spec}\`. Read it first -- it is in your worktree. Follow its acceptance criteria exactly, and treat its 'out of scope' section as binding: other agents are implementing the sibling specs at the same time.${
                agent.intent.trim() ? `\n\nAlso: ${agent.intent.trim()}` : ""
              }`
            : agent.intent,
          provider: agent.provider,
          model: agent.model,
        })),
      }),
    });
    setBusy(false);
    if (!response.ok) {
      setError(((await response.json()) as { error: string }).error);
      return;
    }
    onStarted();
    onClose();
  };

  const ready = name.trim() && agents.every((agent) => agent.spec || agent.intent.trim());

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl border border-border bg-card shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-center gap-2 border-b border-border/60 px-5 py-3.5">
          <Play className="h-4 w-4 text-primary" />
          <h2 className="font-sans text-sm font-medium tracking-[-0.01em]">
            New run in {repoName}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4">
          <label className="block">
            <span className="mb-1.5 block font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              Task name
            </span>
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="what this run is for"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-[13px] outline-none focus:border-primary/60"
            />
          </label>

          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                Agents
              </span>
              <span className="font-mono text-[11px] text-muted-foreground/60">
                one worktree each, running in parallel
              </span>
            </div>

            {agents.map((agent, index) => (
              <div
                key={agent.key}
                className="space-y-2.5 rounded-lg border border-border/60 bg-background/40 p-3"
              >
                <div className="flex items-center gap-2">
                  <span className="flex h-5 w-5 items-center justify-center rounded bg-primary/15 font-mono text-[10px] text-primary">
                    {index + 1}
                  </span>
                  <input
                    value={agent.title}
                    onChange={(event) => update(agent.key, { title: event.target.value })}
                    placeholder={`agent ${index + 1}`}
                    className="flex-1 rounded-md border border-transparent bg-transparent px-1.5 py-1 text-[13px] outline-none hover:border-border focus:border-primary/60"
                  />
                  {agents.length > 1 && (
                    <button
                      type="button"
                      onClick={() =>
                        setAgents((current) => current.filter((item) => item.key !== agent.key))
                      }
                      className="text-muted-foreground hover:text-destructive"
                      aria-label="remove agent"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>

                {agent.spec && (
                  <div className="flex items-center gap-1.5 rounded-md border border-primary/30 bg-primary/5 px-2 py-1">
                    <FileText className="h-3 w-3 shrink-0 text-primary" />
                    <span className="truncate font-mono text-[11.5px] text-primary">
                      {agent.spec}
                    </span>
                    <button
                      type="button"
                      onClick={() => update(agent.key, { spec: null })}
                      className="ml-auto text-muted-foreground hover:text-foreground"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                )}

                <textarea
                  value={agent.intent}
                  onChange={(event) => update(agent.key, { intent: event.target.value })}
                  rows={agent.spec ? 2 : 3}
                  placeholder={agent.spec ? "extra instructions (optional)" : PROMPT_PLACEHOLDER}
                  className="w-full resize-y rounded-md border border-border bg-background px-2.5 py-2 text-[13px] outline-none placeholder:text-muted-foreground/50 focus:border-primary/60"
                />

                <div className="flex flex-wrap items-center gap-2">
                  {specs.length > 0 && (
                    <select
                      value={agent.spec ?? ""}
                      onChange={(event) =>
                        update(agent.key, { spec: event.target.value || null })
                      }
                      className="rounded-md border border-border bg-background px-2 py-1 font-mono text-[11.5px] outline-none focus:border-primary/60"
                    >
                      <option value="">use a spec…</option>
                      {specs.map((path) => (
                        <option key={path} value={path}>
                          {path}
                        </option>
                      ))}
                    </select>
                  )}
                  <select
                    value={agent.provider}
                    onChange={(event) => update(agent.key, { provider: event.target.value })}
                    className="rounded-md border border-border bg-background px-2 py-1 font-mono text-[11.5px] outline-none focus:border-primary/60"
                  >
                    {[...new Set(models.map((item) => item.provider))].map((provider) => (
                      <option key={provider} value={provider}>
                        {provider}
                      </option>
                    ))}
                  </select>
                  <select
                    value={agent.model}
                    onChange={(event) => update(agent.key, { model: event.target.value })}
                    className="rounded-md border border-border bg-background px-2 py-1 font-mono text-[11.5px] outline-none focus:border-primary/60"
                  >
                    {models
                      .filter((item) => item.provider === agent.provider)
                      .map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.label}
                        </option>
                      ))}
                  </select>
                  <span className="truncate font-mono text-[10.5px] text-muted-foreground/60">
                    {models.find((item) => item.id === agent.model)?.note}
                  </span>
                </div>
              </div>
            ))}

            <button
              type="button"
              onClick={() =>
                setAgents((current) => [
                  ...current,
                  blank(models.find((item) => item.default)?.id ?? "claude-opus-5"),
                ])
              }
              className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-border py-2 text-[12px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-primary"
            >
              <Plus className="h-3.5 w-3.5" />
              Add agent
            </button>
          </div>
        </div>

        <footer className="space-y-2 border-t border-border/60 px-5 py-3">
          {error && <p className="text-[12px] text-destructive">{error}</p>}
          <div className="flex items-center gap-2">
            <select
              value={runtime}
              onChange={(event) => setRuntime(event.target.value)}
              className="rounded-md border border-border bg-background px-2 py-1.5 font-mono text-[11.5px] outline-none focus:border-primary/60"
            >
              <option value="claude_code">claude_code — flat-rate plan</option>
              <option value="pydantic_ai">pydantic_ai — metered API</option>
            </select>
            <span className="font-mono text-[10.5px] text-muted-foreground/60">
              {agents.length} worktree{agents.length === 1 ? "" : "s"}
            </span>
            <button
              type="button"
              disabled={!ready || busy}
              onClick={() => void start()}
              className={cn(
                "ml-auto inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground",
                (!ready || busy) && "opacity-40",
              )}
            >
              <Play className="h-3 w-3" />
              {busy ? "starting…" : "Start run"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
