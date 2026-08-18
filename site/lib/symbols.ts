/**
 * Signature extraction for token hover.
 *
 * The point of hovering a token in this project is to see the type, because the
 * codebase is type-driven and the signature is the most informative thing about any
 * name in it. This is a reader, not a type checker: it reports what the source says.
 */

export type Symbol = {
  name: string;
  kind: "function" | "class" | "field" | "variable";
  signature: string;
  line: number;
};

const PY_DEF = /^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/;
const PY_CLASS = /^(\s*)class\s+([A-Za-z_]\w*)\s*[(:]/;
const PY_FIELD = /^(\s+)([A-Za-z_]\w*)\s*:\s*([^=]+?)(?:\s*=\s*(.+))?$/;
const TS_FN = /^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*[(<]/;
const TS_DECL = /^\s*(?:export\s+)?(?:const|let|class|type|interface)\s+([A-Za-z_$][\w$]*)/;

/** Join a definition that wraps across lines, up to its closing colon. */
function joinDefinition(lines: string[], start: number): string {
  let depth = 0;
  const parts: string[] = [];
  for (let index = start; index < Math.min(lines.length, start + 40); index += 1) {
    const line = lines[index];
    parts.push(line.trim());
    for (const char of line) {
      if ("([{".includes(char)) depth += 1;
      else if (")]}".includes(char)) depth -= 1;
    }
    if (depth <= 0 && line.trimEnd().endsWith(":")) break;
  }
  return parts.join(" ").replace(/\s+/g, " ").replace(/:$/, "");
}

export function extractSymbols(contents: string, file: string): Record<string, Symbol> {
  const found: Record<string, Symbol> = {};
  const lines = contents.split("\n");
  const python = file.endsWith(".py");

  lines.forEach((line, index) => {
    if (python) {
      const def = PY_DEF.exec(line);
      if (def) {
        found[def[2]] ??= {
          name: def[2],
          kind: "function",
          signature: joinDefinition(lines, index),
          line: index + 1,
        };
        return;
      }
      const cls = PY_CLASS.exec(line);
      if (cls) {
        found[cls[2]] ??= {
          name: cls[2],
          kind: "class",
          signature: joinDefinition(lines, index),
          line: index + 1,
        };
        return;
      }
      const field = PY_FIELD.exec(line);
      if (field && !line.trimStart().startsWith("#")) {
        found[field[2]] ??= {
          name: field[2],
          kind: "field",
          signature: `${field[2]}: ${field[3].trim()}`,
          line: index + 1,
        };
      }
      return;
    }

    const fn = TS_FN.exec(line) ?? TS_DECL.exec(line);
    if (fn) {
      found[fn[1]] ??= {
        name: fn[1],
        kind: "variable",
        signature: line.trim().replace(/\s*\{\s*$/, ""),
        line: index + 1,
      };
    }
  });

  return found;
}
