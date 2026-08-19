import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Every place a person types.
 *
 * Text, prose and choice used to be spelled out at each call site, which is why one
 * input had `py-1.5` and the next `py-2` and a third a different focus colour. One ring,
 * one radius, one height — the difference between them is what they hold, nothing else.
 */
const CONTROL =
  "w-full rounded-md border border-border bg-background text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground/60 focus:border-ring disabled:opacity-40";

function Input({ className, ...props }: React.ComponentProps<"input">) {
  return <input data-slot="input" className={cn(CONTROL, "h-8 px-2.5", className)} {...props} />;
}

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(CONTROL, "resize-y px-2.5 py-2 leading-relaxed", className)}
      {...props}
    />
  );
}

function Select({ className, ...props }: React.ComponentProps<"select">) {
  return (
    <select
      data-slot="select"
      className={cn(CONTROL, "h-7 w-auto px-2 font-mono text-xs", className)}
      {...props}
    />
  );
}

/**
 * The label above a control, and the only place small-caps type is allowed.
 *
 * Uppercase at this size is a texture rather than a word, which is what makes it read as
 * a boundary between sections instead of competing with the content under it.
 */
function FieldLabel({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="field-label"
      className={cn(
        "block font-mono text-2xs uppercase tracking-wider text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

export { FieldLabel, Input, Select, Textarea };
