"use client";

import { useEffect } from "react";

/**
 * One notification for "the set of projects changed".
 *
 * The nav and the page each hold their own view of which projects exist, and each
 * caller of the add dialog was left to refresh both. They did it differently, so adding
 * from the sidebar updated everything and adding from the empty state updated only half
 * the screen. The dialog now announces the change and every view listens, so there is
 * one behaviour rather than one per caller.
 */
const EVENT = "lj:repos-changed";

export function notifyReposChanged(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(EVENT));
}

export function useReposChanged(onChange: () => void): void {
  useEffect(() => {
    window.addEventListener(EVENT, onChange);
    return () => window.removeEventListener(EVENT, onChange);
  }, [onChange]);
}
