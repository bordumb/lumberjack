"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { FolderPlus } from "lucide-react";
import { AddRepo } from "@/components/add-repo";

/**
 * The empty state has to do something.
 *
 * Telling a reader to use a control in the sidebar makes them go looking for it; the
 * screen they are already on should be able to start the job.
 */
export function NoProjects() {
  const [adding, setAdding] = useState(false);
  const router = useRouter();

  return (
    <main className="mx-auto flex max-w-xl flex-col items-center px-8 py-24 text-center">
      <h1 className="font-sans text-xl font-semibold tracking-[-0.02em]">No projects</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Point the dashboard at a git repository to see its files and its runs.
      </p>

      <button
        type="button"
        onClick={() => setAdding(true)}
        className="mt-6 inline-flex items-center gap-2 rounded-lg bg-foreground px-5 py-2.5 text-[14px] font-medium text-background shadow-sm transition-opacity hover:opacity-90"
      >
        <FolderPlus className="h-4 w-4" />
        Add project
      </button>

      <p className="mt-6 text-[12px] text-muted-foreground/70">
        Removing a project never deletes anything on disk, so adding it back brings its
        runs with it.
      </p>

      {adding && (
        <AddRepo onClose={() => setAdding(false)} onAdded={() => router.refresh()} />
      )}
    </main>
  );
}
