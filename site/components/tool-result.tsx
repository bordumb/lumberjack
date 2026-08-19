"use client";

import { useState } from "react";
import {
  AlertTriangle,
  ChevronRight,
  GitBranch,
  MessageSquare,
  ShieldAlert,
  StickyNote,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { CodeSnippet } from "@/components/code-snippet";
import { interpret, weight } from "@/lib/tool-result";
import type { DigestSection } from "@/lib/tool-result";
import { cn } from "@/lib/utils";

const COLLAPSE_OVER = 5;

/** The digest's own sections, in the order it emits them. */
const SECTIONS: { match: string; icon: LucideIcon; tone: string }[] = [
  { match: "HUMAN REVIEW", icon: MessageSquare, tone: "text-primary" },
  { match: "OPEN CONFLICTS", icon: AlertTriangle, tone: "text-amber-400" },
  { match: "MESSAGES", icon: MessageSquare, tone: "text-sky-400" },
  { match: "PROTOCOL", icon: ShieldAlert, tone: "text-amber-400" },
  { match: "PEERS", icon: Users, tone: "text-muted-foreground" },
  { match: "FROZEN CONTRACTS", icon: GitBranch, tone: "text-muted-foreground" },
  { match: "INTEGRATION", icon: GitBranch, tone: "text-muted-foreground" },
  { match: "BLACKBOARD", icon: StickyNote, tone: "text-muted-foreground" },
];

function look(title: string) {
  return SECTIONS.find((item) => title.startsWith(item.match)) ?? SECTIONS[SECTIONS.length - 1];
}

const SEVERITY: Record<string, string> = {
  "[block]": "text-destructive",
  "[warn]": "text-amber-400",
  "[notice]": "text-muted-foreground",
};

function severityOf(item: string): string | undefined {
  return Object.keys(SEVERITY).find((key) => item.startsWith(key));
}

export function ToolResultView({ raw }: { raw: string }) {
  const result = interpret(raw);
  const lines = weight(result);
  const long = lines > COLLAPSE_OVER;
  const [open, setOpen] = useState(!long);

  if (result.kind === "text") {
    return <CodeSnippet code={result.text} language="text" />;
  }

  const summary =
    result.kind === "digest"
      ? result.sections.map((section) => `${section.items.length} ${section.title.split(" ")[0].toLowerCase()}`).join(" · ")
      : `${lines} entries`;

  const body = (
    <div className="space-y-2.5 px-3 py-2">
      {result.kind === "digest" &&
        (result.sections.length === 0 ? (
          <p className="text-[12.5px] text-muted-foreground">{result.preamble}</p>
        ) : (
          result.sections.map((section) => <Section key={section.title} section={section} />)
        ))}

      {result.kind === "fields" && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {result.fields.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="font-mono text-[11px] text-muted-foreground">{key}</dt>
              <dd className="font-mono text-[11.5px] text-foreground/85">{value || "—"}</dd>
            </div>
          ))}
        </dl>
      )}

      {result.kind === "lines" && (
        <ul className="space-y-0.5">
          {result.lines.map((line, index) => (
            <li key={index} className="font-mono text-[11.5px] text-foreground/85">
              {line}
            </li>
          ))}
        </ul>
      )}
    </div>
  );

  if (!long) {
    return <div className="mt-2 rounded-md border border-border/50 bg-card/30">{body}</div>;
  }

  return (
    <div className="mt-2 overflow-hidden rounded-md border border-border/50 bg-card/30">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 border-b border-border/50 bg-muted/20 px-2.5 py-1.5 text-left text-[11px] text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
      >
        <ChevronRight className={cn("h-3 w-3 transition-transform", open && "rotate-90")} />
        <span className="truncate font-mono">{summary}</span>
        <span className="ml-auto shrink-0 opacity-60">{open ? "collapse" : "expand"}</span>
      </button>
      {open && body}
    </div>
  );
}

function Section({ section }: { section: DigestSection }) {
  const { icon: Icon, tone } = look(section.title);
  const [title, note] = section.title.split(" -- ");

  return (
    <section>
      <div className="mb-1 flex items-center gap-1.5">
        <Icon className={cn("h-3 w-3 shrink-0", tone)} />
        <span className={cn("font-mono text-[10.5px] uppercase tracking-wider", tone)}>
          {title}
        </span>
        {note && (
          <span className="truncate font-mono text-[10.5px] text-muted-foreground/60">
            {note}
          </span>
        )}
      </div>
      <ul className="space-y-1 pl-4">
        {section.items.map((item, index) => {
          const severity = severityOf(item);
          return (
            <li
              key={index}
              className={cn(
                "whitespace-pre-wrap font-mono text-[11.5px] text-foreground/85",
                severity && SEVERITY[severity],
              )}
            >
              {item}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
