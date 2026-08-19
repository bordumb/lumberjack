"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, CircleDot, MessageSquarePlus, Send, Undo2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { CommentStatus, ReviewComment } from "@/lib/types";

const STATUS: Record<CommentStatus, { label: string; className: string }> = {
  queued: { label: "queued", className: "border-amber-500/40 text-amber-400" },
  delivered: { label: "delivered", className: "border-sky-500/40 text-sky-400" },
  addressed: { label: "addressed", className: "border-emerald-500/40 text-emerald-400" },
  resolved: { label: "resolved", className: "border-border text-muted-foreground" },
};

const EXPLAIN: Record<CommentStatus, string> = {
  queued: "waiting for the agent to read its inbox",
  delivered: "the agent has read it",
  addressed: "the agent has edited this file since",
  resolved: "closed; no longer blocking a land",
};

export type Target = {
  file: string;
  lineStart: number;
  lineEnd: number;
  side?: string;
  workstream?: string;
  conflict?: string;
};

/** Live comments for a stand, refreshed alongside everything else on the page. */
export function useComments(stand: string) {
  const [comments, setComments] = useState<ReviewComment[]>([]);

  const refresh = useCallback(async () => {
    const response = await fetch(`/api/comments?stand=${stand}`);
    const data = (await response.json()) as { comments: ReviewComment[] };
    setComments(data.comments);
  }, [stand]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [refresh]);

  const post = useCallback(
    async (target: Target, body: string) => {
      await fetch("/api/comments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stand, ...target, line: target.lineStart, body }),
      });
      await refresh();
    },
    [stand, refresh],
  );

  const resolve = useCallback(
    async (comment: string) => {
      await fetch("/api/comments/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stand, comment }),
      });
      await refresh();
    },
    [stand, refresh],
  );

  return { comments, post, resolve, refresh };
}

export function CommentComposer({
  target,
  onSubmit,
  onCancel,
}: {
  target: Target;
  onSubmit: (body: string) => Promise<void> | void;
  onCancel: () => void;
}) {
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const lines =
    target.lineStart === target.lineEnd
      ? `line ${target.lineStart}`
      : `lines ${target.lineStart}-${target.lineEnd}`;

  const send = async () => {
    if (!body.trim() || sending) return;
    setSending(true);
    await onSubmit(body.trim());
    setSending(false);
    setBody("");
  };

  return (
    <div className="rounded-lg border border-primary/40 bg-card p-3">
      <div className="mb-2 flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
        <MessageSquarePlus className="h-3.5 w-3.5 text-primary" />
        {target.file}:{lines}
        <span className="ml-auto">
          {target.conflict ? "goes to both sides of the conflict" : "goes to this agent"}
        </span>
      </div>
      <textarea
        autoFocus
        value={body}
        onChange={(event) => setBody(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void send();
        }}
        rows={3}
        placeholder="What should the agent do differently? This blocks the work from landing until resolved."
        className="w-full resize-y rounded-md border border-border bg-background px-2.5 py-2 text-[13px] outline-none focus:border-primary/60"
      />
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={() => void send()}
          disabled={!body.trim() || sending}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
        >
          <Send className="h-3 w-3" />
          {sending ? "sending" : "comment"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-2.5 py-1 text-[12px] text-muted-foreground hover:text-foreground"
        >
          cancel
        </button>
        <span className="ml-auto font-mono text-[10.5px] text-muted-foreground/60">⌘↵ to send</span>
      </div>
    </div>
  );
}

export function CommentCard({
  comment,
  onResolve,
}: {
  comment: ReviewComment;
  onResolve: (id: string) => Promise<void> | void;
}) {
  const status = STATUS[comment.status];
  return (
    <div
      className={cn(
        "rounded-lg border bg-card/60 p-3",
        comment.resolved ? "border-border/40 opacity-60" : "border-primary/30",
      )}
    >
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="h-5 border-primary/40 text-[10px] text-primary">
          {comment.author}
        </Badge>
        <Badge variant="outline" className={cn("h-5 text-[10px]", status.className)}>
          {status.label}
        </Badge>
        <span className="font-mono text-[10.5px] text-muted-foreground/70">
          {EXPLAIN[comment.status]}
        </span>
        {!comment.resolved && (
          <button
            type="button"
            onClick={() => void onResolve(comment.id)}
            className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:border-emerald-500/40 hover:text-emerald-400"
          >
            <Check className="h-3 w-3" />
            resolve
          </button>
        )}
      </div>
      <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground/90">
        {comment.body}
      </p>
      {comment.notified.length > 0 && (
        <p className="mt-1.5 font-mono text-[10.5px] text-muted-foreground/60">
          sent to {comment.notified.join(", ")}
        </p>
      )}
      {comment.replies.map((reply, index) => (
        <div key={index} className="mt-2 border-l-2 border-border/60 pl-2.5">
          <div className="flex items-center gap-1.5 font-mono text-[10.5px] text-muted-foreground">
            <Undo2 className="h-3 w-3" />
            {reply.frm}
          </div>
          <p className="text-[12.5px] text-foreground/80">{reply.body}</p>
        </div>
      ))}
    </div>
  );
}

export function CommentPin({ count }: { count: number }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-primary/40 bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-primary">
      <CircleDot className="h-2.5 w-2.5" />
      {count}
    </span>
  );
}
