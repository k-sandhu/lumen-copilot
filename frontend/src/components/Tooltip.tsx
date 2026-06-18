/**
 * Shared Radix tooltip primitive (app-agnostic, presentational). Extracted so
 * features (citations, etc.) reuse one accessible tooltip instead of re-wiring
 * Radix each time. A single Provider is mounted at the app root.
 */
import * as RadixTooltip from '@radix-ui/react-tooltip';
import type { ComponentProps, ReactNode } from 'react';
import { cn } from '@/lib/cn';

export const TooltipProvider = RadixTooltip.Provider;
export const Tooltip = RadixTooltip.Root;
export const TooltipTrigger = RadixTooltip.Trigger;

export function TooltipContent({
  children,
  className,
  sideOffset = 6,
  ...rest
}: { children: ReactNode } & ComponentProps<typeof RadixTooltip.Content>) {
  return (
    <RadixTooltip.Portal>
      <RadixTooltip.Content
        sideOffset={sideOffset}
        className={cn(
          'z-50 max-w-sm rounded-md border border-border bg-surface px-2 py-1 text-xs text-foreground shadow-md',
          className,
        )}
        {...rest}
      >
        {children}
        <RadixTooltip.Arrow className="fill-border" />
      </RadixTooltip.Content>
    </RadixTooltip.Portal>
  );
}
