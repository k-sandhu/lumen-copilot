import * as RadixScrollArea from '@radix-ui/react-scroll-area';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface ScrollAreaProps {
  children: ReactNode;
  className?: string;
  /** Class for the inner viewport (where content lives). */
  viewportClassName?: string;
}

/**
 * Radix-backed scroll container. The KEY to the multi-pane layout: it sets
 * `h-full` and the viewport handles overflow, so a pane scrolls INDEPENDENTLY
 * inside a flex/grid cell that uses `min-h-0`. Long content never forces a
 * whole-page scroll (frontend/AGENTS.md "Independently scrollable panes").
 */
export function ScrollArea({ children, className, viewportClassName }: ScrollAreaProps) {
  return (
    <RadixScrollArea.Root className={cn('h-full overflow-hidden', className)} type="hover">
      <RadixScrollArea.Viewport className={cn('h-full w-full', viewportClassName)}>
        {children}
      </RadixScrollArea.Viewport>
      <RadixScrollArea.Scrollbar
        orientation="vertical"
        className="flex w-2 touch-none select-none bg-transparent p-0.5"
      >
        <RadixScrollArea.Thumb className="flex-1 rounded-full bg-foreground-muted/40" />
      </RadixScrollArea.Scrollbar>
      <RadixScrollArea.Scrollbar
        orientation="horizontal"
        className="flex h-2 touch-none select-none bg-transparent p-0.5"
      >
        <RadixScrollArea.Thumb className="flex-1 rounded-full bg-foreground-muted/40" />
      </RadixScrollArea.Scrollbar>
      <RadixScrollArea.Corner />
    </RadixScrollArea.Root>
  );
}
