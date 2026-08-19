import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { CommentStatus, Lifecycle } from "@/lib/types";

/**
 * What every colour on this interface means.
 *
 * Four tones, and each one is a claim about the world rather than a hue: `good` is
 * working or agreed, `warn` needs a person eventually, `bad` needs one now, and `info`
 * is in flight. Components ask for a tone; only this file knows which token draws it.
 *
 * The rule this replaces was no rule: `emerald-400` here, `amber-500/15` there,
 * `sky-600` on one button, each decided at the call site and impossible to change from
 * one place.
 */
export type Tone = "good" | "warn" | "bad" | "info" | "mute";

const TEXT: Record<Tone, string> = {
  good: "text-success",
  warn: "text-warning",
  bad: "text-destructive",
  info: "text-info",
  mute: "text-muted-foreground",
};

const DOT: Record<Tone, string> = {
  good: "bg-success",
  warn: "bg-warning",
  bad: "bg-destructive",
  info: "bg-info",
  mute: "bg-muted-foreground",
};

/**
 * A tinted badge: the tone as a hairline and a whisper of fill, never a solid block.
 * Filled status chips are the fastest way to make a dense page look like an error log.
 */
const CHIP: Record<Tone, string> = {
  good: "border-success/30 bg-success/10 text-success",
  warn: "border-warning/30 bg-warning/10 text-warning",
  bad: "border-destructive/30 bg-destructive/10 text-destructive",
  info: "border-info/30 bg-info/10 text-info",
  mute: "border-border bg-transparent text-muted-foreground",
};

export const LIFECYCLE_TONE: Record<Lifecycle, Tone> = {
  live: "good",
  stale: "warn",
  halted: "bad",
  finished: "mute",
};

export const COMMENT_TONE: Record<CommentStatus, Tone> = {
  queued: "warn",
  delivered: "info",
  addressed: "good",
  resolved: "mute",
};

/** A workstream's state, which is a lifecycle of its own. */
export function taskTone(state: string): Tone {
  if (state === "running") return "good";
  if (state === "awaiting_integration") return "info";
  if (state === "blocked") return "bad";
  return "mute";
}

export function severityTone(severity: string): Tone {
  if (severity === "block") return "bad";
  if (severity === "warn") return "warn";
  return "mute";
}

export function toneText(tone: Tone): string {
  return TEXT[tone];
}

/** The smallest possible statement of state: six pixels of colour beside a name. */
export function StatusDot({ tone, className }: { tone: Tone; className?: string }) {
  return <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", DOT[tone], className)} />;
}

/** A word plus its tone. The only badge variant that carries meaning. */
export function StatusBadge({
  tone,
  className,
  children,
}: {
  tone: Tone;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Badge variant="outline" className={cn("font-mono text-2xs", CHIP[tone], className)}>
      {children}
    </Badge>
  );
}

/** A count or an id: mono, outlined, no tone. Most badges are this one. */
export function Chip({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <Badge variant="outline" className={cn("font-mono text-2xs text-muted-foreground", className)}>
      {children}
    </Badge>
  );
}
