"use client";

import { useCallback, useState } from "react";
import type { CSSProperties } from "react";

export type SymbolInfo = {
  name: string;
  kind: "function" | "class" | "field" | "variable";
  signature: string;
  line: number;
};

const KIND_LABEL: Record<SymbolInfo["kind"], string> = {
  function: "def",
  class: "class",
  field: "field",
  variable: "decl",
};

const CARD_WIDTH = 640;
const CARD_HEIGHT = 92;
const MARGIN = 12;

type Hovered = { symbol: SymbolInfo; top: number; left: number; width: number };

/**
 * Token hover for a type-driven codebase: point at a name, see its signature.
 *
 * Wired through the diff renderer's own token events rather than a DOM listener, so
 * it survives virtualization -- rows are recycled as you scroll and any handler
 * attached to a row goes with them.
 */
export function useTokenHover(symbols: Record<string, SymbolInfo>) {
  const [hovered, setHovered] = useState<Hovered | null>(null);

  const onTokenEnter = useCallback(
    (props: { tokenText: string; tokenElement: HTMLElement }) => {
      const symbol = symbols[props.tokenText.trim()];
      if (!symbol) return;
      const rect = props.tokenElement.getBoundingClientRect();
      // Keep the card on screen: tokens near the right edge are common in a split
      // diff, and a signature that runs off the viewport explains nothing.
      const width = Math.min(CARD_WIDTH, window.innerWidth - MARGIN * 2);
      const left = Math.min(Math.max(MARGIN, rect.left), window.innerWidth - width - MARGIN);
      const flipUp = rect.bottom + CARD_HEIGHT > window.innerHeight;
      setHovered({
        symbol,
        top: flipUp ? rect.top - CARD_HEIGHT - 6 : rect.bottom + 6,
        left,
        width,
      });
    },
    [symbols],
  );

  const onTokenLeave = useCallback(() => setHovered(null), []);

  return { hovered, onTokenEnter, onTokenLeave };
}

export function TokenHoverCard({ hovered }: { hovered: Hovered | null }) {
  if (!hovered) return null;
  const style: CSSProperties = {
    top: hovered.top,
    left: hovered.left,
    width: hovered.width,
  };
  return (
    <div
      className="pointer-events-none fixed z-50 rounded-lg border border-border bg-popover px-3 py-2 shadow-xl"
      style={style}
    >
      <div className="mb-1 flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
        <span className="rounded bg-primary/15 px-1.5 py-0.5 text-primary">
          {KIND_LABEL[hovered.symbol.kind]}
        </span>
        <span>line {hovered.symbol.line}</span>
      </div>
      <code className="block break-words whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-foreground/90">
        {hovered.symbol.signature}
      </code>
    </div>
  );
}
