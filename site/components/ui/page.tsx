import * as React from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * The page frame, and the two headings a page is allowed.
 *
 * Every route had rewritten the same measure, the same gutter and the same
 * `text-xl font-semibold tracking-[-0.02em]`, which meant a heading was one careless
 * paste away from being a different size on the next page over.
 */

const WIDTH = {
  /* the run dashboard and the conflict pages: dense, tabular, wide */
  wide: "max-w-6xl",
  /* a transcript: long lines are unreadable, so it stops sooner */
  default: "max-w-5xl",
  /* prose */
  narrow: "max-w-3xl",
} as const;

function Page({
  width = "default",
  className,
  ...props
}: React.ComponentProps<"main"> & { width?: keyof typeof WIDTH }) {
  return <main className={cn("mx-auto space-y-8 px-8 py-7", WIDTH[width], className)} {...props} />;
}

/** The block at the top of a page: back link, title row, then one line of metadata. */
function PageHeader({ className, ...props }: React.ComponentProps<"header">) {
  return <header className={cn("space-y-2.5", className)} {...props} />;
}

function PageTitle({ className, ...props }: React.ComponentProps<"h1">) {
  return <h1 className={cn("text-xl font-semibold tracking-tighter", className)} {...props} />;
}

function SectionTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return <h2 className={cn("text-lg font-medium tracking-tight", className)} {...props} />;
}

/** The way back up. Always the same place, always the same weight. */
function BackLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link
      href={href}
      className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
    >
      <ArrowLeft className="h-3 w-3" />
      {children}
    </Link>
  );
}

/**
 * Secondary fact: a branch, a path, a count, a time.
 *
 * Mono because these are machine strings and proportional type makes them wander, muted
 * because they are the second thing read. Together they are most of the interface, which
 * is why they get a component instead of a spelled-out class list per site.
 */
function Meta({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("font-mono text-xs text-muted-foreground", className)} {...props} />;
}

export { BackLink, Meta, Page, PageHeader, PageTitle, SectionTitle };
