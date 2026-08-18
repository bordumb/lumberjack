import {
  Bot, BookOpen, ClipboardList, FolderSearch, GitMerge, GitPullRequestArrow,
  ListChecks, Lock, LogIn, MessageSquare, Pencil, PenLine, Radar, Search,
  StickyNote, Terminal, Unlock, Users, Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

const ICONS: Record<string, LucideIcon> = {
  Read: BookOpen,
  Write: PenLine,
  Edit: Pencil,
  Run: Terminal,
  Search: Search,
  Glob: FolderSearch,
  Plan: ListChecks,
  Delegate: Bot,
  join: LogIn,
  awareness: Radar,
  claim: Lock,
  claim_symbols: Lock,
  release: Unlock,
  who_touches: Users,
  check_merge: GitMerge,
  request_land: GitPullRequestArrow,
  post_note: StickyNote,
  read_board: ClipboardList,
  message: MessageSquare,
  blast_radius: Radar,
};

/** Coordination tools are the interesting ones; give them their own colour. */
const COORDINATION = new Set([
  "join", "awareness", "claim", "claim_symbols", "release", "who_touches",
  "check_merge", "request_land", "post_note", "read_board", "message", "blast_radius",
]);

export function toolIcon(label: string): LucideIcon {
  return ICONS[label] ?? Wrench;
}

export function isCoordination(label: string): boolean {
  return COORDINATION.has(label);
}
