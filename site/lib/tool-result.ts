/**
 * What a tool actually returned, rather than the envelope it arrived in.
 *
 * MCP results come back as `{"result": "..."}`, and for the coordination tools the
 * string inside is itself structured -- the awareness digest is rendered from typed
 * sections in `core/digest.py` before the model ever sees it. Printing the JSON throws
 * away both layers, so a digest listing two conflicts and a human comment reads as one
 * long escaped line.
 *
 * This unwraps by shape and never by tool name: a rule keyed on the name goes stale the
 * moment a tool is added, and the failure is silent.
 */

export type DigestSection = { title: string; items: string[] };

export type ToolResult =
  | { kind: "digest"; sections: DigestSection[]; preamble: string }
  | { kind: "fields"; fields: [string, string][] }
  | { kind: "lines"; lines: string[] }
  | { kind: "text"; text: string };

const DIGEST_MARKER = "Swarm awareness";

/** Section headers are upper-case lines; the digest writes them and nothing else does. */
const SECTION = /^[A-Z][A-Z .,'\-]{3,}$/;

function parseDigest(text: string): ToolResult {
  const lines = text.split("\n");
  const sections: DigestSection[] = [];
  const preamble: string[] = [];
  let current: DigestSection | null = null;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const header = trimmed.split(" -- ")[0];
    if (SECTION.test(header) && !trimmed.startsWith("-")) {
      current = { title: trimmed, items: [] };
      sections.push(current);
      continue;
    }
    if (current) {
      // Continuation lines belong to the bullet above them.
      if (trimmed.startsWith("- ")) current.items.push(trimmed.slice(2));
      else if (current.items.length > 0) {
        current.items[current.items.length - 1] += `\n${trimmed}`;
      } else current.items.push(trimmed);
    } else {
      preamble.push(trimmed);
    }
  }
  return { kind: "digest", sections, preamble: preamble.join(" ") };
}

function unwrap(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>);
    const only = entries.length === 1 ? entries[0] : undefined;
    if (only && typeof only[1] === "string") return only[1];
  }
  return null;
}

export function interpret(raw: string): ToolResult {
  const text = raw.trim();
  if (!text) return { kind: "text", text: "" };

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return text.startsWith(DIGEST_MARKER) ? parseDigest(text) : { kind: "text", text };
  }

  const inner = unwrap(parsed);
  if (inner !== null) {
    return inner.startsWith(DIGEST_MARKER) ? parseDigest(inner) : { kind: "text", text: inner };
  }

  if (Array.isArray(parsed)) {
    return { kind: "lines", lines: parsed.map((item) => String(item)) };
  }
  if (parsed && typeof parsed === "object") {
    // A small record reads better as fields than as JSON; a large one does not.
    const fields = Object.entries(parsed as Record<string, unknown>).map(
      ([key, value]) =>
        [key, Array.isArray(value) ? value.join(", ") : String(value)] as [string, string],
    );
    return fields.length <= 8 ? { kind: "fields", fields } : { kind: "text", text };
  }
  return { kind: "text", text };
}

/** How many lines the result occupies, for deciding whether to collapse it. */
export function weight(result: ToolResult): number {
  switch (result.kind) {
    case "digest":
      return result.sections.reduce((total, section) => total + 1 + section.items.length, 0);
    case "fields":
      return result.fields.length;
    case "lines":
      return result.lines.length;
    case "text":
      return result.text.split("\n").length;
  }
}
