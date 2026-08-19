"use client";

import * as React from "react";
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The one dialog.
 *
 * There were two hand-rolled ones — add-project and new-run — with different widths,
 * different header padding and different close buttons, which is two dialogs to learn
 * for one idea. Everything here is fixed except the width and what goes inside.
 */
function Modal({
  icon: Icon,
  title,
  onClose,
  className,
  children,
  footer,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  onClose: () => void;
  className?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  // Escape closes it. A dialog that can only be dismissed with the mouse is a dialog
  // that traps the reader who opened it from the keyboard.
  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-6 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label={title}
        className={cn(
          "flex w-full flex-col overflow-hidden rounded-lg border border-border bg-popover shadow-overlay",
          className,
        )}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-center gap-2 border-b border-border px-4 py-2.5">
          {Icon && <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
          <h2 className="text-sm font-medium tracking-tight">{title}</h2>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="close"
            onClick={onClose}
            className="ml-auto text-muted-foreground"
          >
            <X />
          </Button>
        </header>

        {children}

        {footer && (
          <footer className="space-y-2 border-t border-border px-4 py-2.5">{footer}</footer>
        )}
      </div>
    </div>
  );
}

/** The scrolling middle of a dialog, so its padding is not restated per dialog. */
function ModalBody({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("min-h-0 flex-1 overflow-y-auto px-4 py-3", className)} {...props} />;
}

export { Modal, ModalBody };
