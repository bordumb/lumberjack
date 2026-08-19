"use client";

import { Plus } from "lucide-react";
import { GutterUtilitySlotStyles } from "@pierre/diffs/react";

/**
 * The affordance that makes line comments discoverable: a button in the gutter of the
 * line under the cursor.
 *
 * The renderer owns placement through its slot styles and gives us the hovered line on
 * demand, so the click reads the line at the moment it happens rather than tracking
 * hover state separately.
 */
export function GutterAdd({
  getHoveredLine,
  onPick,
}: {
  getHoveredLine: () => { lineNumber: number } | undefined;
  onPick: (line: number) => void;
}) {
  return (
    <span style={GutterUtilitySlotStyles}>
      <button
        type="button"
        aria-label="comment on this line"
        onClick={(event) => {
          event.stopPropagation();
          const hovered = getHoveredLine();
          if (hovered) onPick(hovered.lineNumber);
        }}
        className="flex h-4 w-4 items-center justify-center rounded-[4px] bg-primary text-primary-foreground shadow-sm transition-transform hover:scale-110"
      >
        <Plus className="h-3 w-3" strokeWidth={3} />
      </button>
    </span>
  );
}
