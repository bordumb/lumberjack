import { readFileSync } from "node:fs";
import type { LogEntry } from "./types";

type Block = {
  type: string;
  text?: string;
  name?: string;
  input?: Record<string, unknown>;
  content?: unknown;
  is_error?: boolean;
};

const EXT_LANGUAGE: Record<string, string> = {
  py: "python", ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx",
  json: "json", md: "markdown", toml: "toml", yml: "yaml", yaml: "yaml",
  sh: "bash", css: "css", html: "html", sql: "sql", rs: "rust", go: "go",
};

function languageOf(file: string): string {
  return EXT_LANGUAGE[file.split(".").pop()?.toLowerCase() ?? ""] ?? "text";
}

function short(value: unknown, limit = 120): string {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/** Strip the worktree prefix: every path in a session starts with it and it is noise. */
function relative(file: string, worktree: string): string {
  return file.startsWith(worktree) ? file.slice(worktree.length).replace(/^\//, "") : file;
}

function describe(name: string, input: Record<string, unknown>, worktree: string) {
  const file = typeof input.file_path === "string" ? relative(input.file_path, worktree) : "";
  switch (name) {
    case "Read":
      return { label: "Read", target: file, body: null, language: languageOf(file) };
    case "Write":
      return {
        label: "Write", target: file,
        body: typeof input.content === "string" ? input.content : null,
        language: languageOf(file),
      };
    case "Edit":
    case "MultiEdit":
      return {
        label: "Edit", target: file,
        body: typeof input.new_string === "string" ? input.new_string : null,
        language: languageOf(file),
      };
    case "Bash":
      return {
        label: "Run", target: short(input.description ?? input.command, 80),
        body: typeof input.command === "string" ? input.command : null,
        language: "bash",
      };
    case "Grep":
    case "Glob":
      return {
        label: name === "Grep" ? "Search" : "Glob",
        target: short(input.pattern, 80), body: null, language: "text",
      };
    case "TodoWrite":
      return { label: "Plan", target: `${(input.todos as unknown[])?.length ?? 0} items`, body: null, language: "text" };
    case "Task":
      return { label: "Delegate", target: short(input.description, 80), body: null, language: "text" };
    default: {
      if (name.startsWith("mcp__lumberjack__")) {
        const tool = name.replace("mcp__lumberjack__", "");
        const target =
          tool === "claim" ? `${short(input.patterns, 60)} (${input.mode ?? ""})`
          : tool === "post_note" ? short(input.topic, 40)
          : tool === "message" ? `to ${short(input.to, 40)}`
          : tool === "who_touches" ? short(input.paths, 60)
          : tool === "check_merge" ? short(input.against ?? "integration", 40)
          : short(input.agent ?? "", 40);
        const body =
          tool === "post_note" && typeof input.body === "string" ? input.body :
          tool === "message" && typeof input.body === "string" ? input.body : null;
        return { label: tool, target, body, language: "text" };
      }
      return { label: name, target: short(input, 80), body: null, language: "text" };
    }
  }
}

function resultText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((item) => (typeof item === "object" && item && "text" in item ? String((item as { text: unknown }).text) : ""))
      .join("\n");
  }
  return "";
}

export function parseTranscript(file: string, worktree: string, limit = 400): LogEntry[] {
  let raw: string;
  try {
    raw = readFileSync(file, "utf8");
  } catch {
    return [];
  }

  const entries: LogEntry[] = [];
  let seq = 0;

  for (const line of raw.split("\n")) {
    if (!line.trim()) continue;
    let event: { type?: string; message?: { role?: string; content?: unknown }; timestamp?: string };
    try {
      event = JSON.parse(line);
    } catch {
      continue;
    }
    const at = event.timestamp ? Date.parse(event.timestamp) : null;
    const content = event.message?.content;
    if (!Array.isArray(content)) {
      if (typeof content === "string" && content.trim() && event.message?.role === "assistant") {
        entries.push({ seq: seq++, role: "assistant", at, text: content });
      }
      continue;
    }

    for (const block of content as Block[]) {
      if (block.type === "text" && block.text?.trim()) {
        entries.push({ seq: seq++, role: "assistant", at, text: block.text });
      } else if (block.type === "tool_use" && block.name) {
        const described = describe(block.name, block.input ?? {}, worktree);
        entries.push({
          seq: seq++, role: "assistant", at,
          tool: { name: block.name, ...described },
        });
      } else if (block.type === "tool_result") {
        const text = resultText(block.content);
        entries.push({
          seq: seq++, role: "user", at,
          result: {
            ok: !block.is_error,
            preview: text.slice(0, 4000),
            lines: text ? text.split("\n").length : 0,
          },
        });
      }
    }
  }
  return entries.slice(-limit);
}
