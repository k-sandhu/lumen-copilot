/**
 * Answer-bubble footer (#120, wireframe chat.html "answer-meta"): the row beneath
 * a completed assistant answer carrying two trust signals and three actions —
 *
 *   - "Permission-checked" status. Honest by construction: the backend only ever
 *     returns sources the requesting user may see (spec 0004 INV-2/INV-3), so a
 *     grounded answer is permission-checked. Shown only when the answer is
 *     actually grounded in at least one cited source — never as a bare claim.
 *   - A freshness timestamp ("freshest <ago>"), derived from the real message
 *     timestamp (presentation.relativeTime). Omitted when we have no timestamp.
 *   - Helpful / Not-helpful — LOCAL-ONLY UI (toggle + aria-pressed). There is NO
 *     backend feedback endpoint, so this persists NOTHING and the component never
 *     implies it does (honest per #120). It is a one-of toggle the user can clear.
 *   - Copy — client-side only (navigator.clipboard), copies the answer text.
 *
 * Pure/presentational: the parent owns the answer text + derived signals; this
 * renders them and the local feedback/copy affordances. No I/O beyond the
 * clipboard write and no contract dependency.
 */
import { useCallback, useRef, useState, type ReactNode } from 'react';
import { Icon } from '@/ui';
import { cn } from '@/lib/cn';

/** Local-only feedback vote — never sent anywhere (no backend endpoint). */
type Vote = 'up' | 'down' | null;

export interface AnswerFooterProps {
  /** The rendered answer text, copied verbatim by the Copy action. */
  answerText: string;
  /** True when the answer is grounded in ≥1 cited source → "Permission-checked". */
  permissionChecked: boolean;
  /** Freshness label for the answer's evidence, e.g. "2d ago". Omitted if absent. */
  freshness?: string | undefined;
}

/** Thumb / copy / check glyphs the shared kit Icon set doesn't carry (#120). */
function FooterGlyph({ name }: { name: 'thumb-up' | 'thumb-down' | 'copy' | 'check' }): ReactNode {
  const common = {
    viewBox: '0 0 24 24',
    className: 'h-3.5 w-3.5',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    focusable: false as const,
  };
  switch (name) {
    case 'thumb-up':
      return (
        <svg {...common}>
          <path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3Zm0 0 4-8a2 2 0 0 1 2 2v4h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 20H7" />
        </svg>
      );
    case 'thumb-down':
      return (
        <svg {...common}>
          <path d="M17 13V4h3a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-3Zm0 0-4 8a2 2 0 0 1-2-2v-4H6a2 2 0 0 1-2-2.3l1.2-6A2 2 0 0 1 7.2 4H17" />
        </svg>
      );
    case 'copy':
      return (
        <svg {...common}>
          <path d="M9 9h10v12H9V9Zm0 0V3h6l4 4M5 15H3V3h12v2" />
        </svg>
      );
    case 'check':
      return (
        <svg {...common}>
          <path d="m5 12 5 5 9-11" />
        </svg>
      );
  }
}

const BTN =
  'inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-foreground-muted transition-colors hover:bg-surface-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent';

export function AnswerFooter({ answerText, permissionChecked, freshness }: AnswerFooterProps) {
  const [vote, setVote] = useState<Vote>(null);
  const [copied, setCopied] = useState(false);
  const copyResetRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Toggle the local-only vote (clicking the active one clears it).
  const toggleVote = useCallback(
    (next: Exclude<Vote, null>) => setVote((prev) => (prev === next ? null : next)),
    [],
  );

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(answerText);
      setCopied(true);
      if (copyResetRef.current) clearTimeout(copyResetRef.current);
      copyResetRef.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard denied/unavailable: leave the icon unchanged — no false success.
      setCopied(false);
    }
  }, [answerText]);

  // Nothing to show? Don't render an empty footer rule.
  if (!permissionChecked && !freshness) return null;

  return (
    <footer className="mt-2 flex items-center gap-3 border-t border-border pt-2 text-[11px] text-foreground-muted">
      {permissionChecked && (
        <span className="inline-flex items-center gap-1 text-ok" title="Answered only from sources you can access">
          <Icon name="shield-check" className="h-3.5 w-3.5" />
          Permission-checked
        </span>
      )}
      {freshness && (
        <span className="inline-flex items-center gap-1">
          <Icon name="clock" className="h-3.5 w-3.5" />
          freshest {freshness}
        </span>
      )}

      <div className="ml-auto flex items-center gap-0.5">
        <button
          type="button"
          aria-pressed={vote === 'up'}
          aria-label="Mark this answer helpful"
          title="Helpful"
          onClick={() => toggleVote('up')}
          className={cn(BTN, vote === 'up' && 'bg-accent/15 text-accent')}
        >
          <FooterGlyph name="thumb-up" />
        </button>
        <button
          type="button"
          aria-pressed={vote === 'down'}
          aria-label="Mark this answer not helpful"
          title="Not helpful"
          onClick={() => toggleVote('down')}
          className={cn(BTN, vote === 'down' && 'bg-danger/15 text-danger')}
        >
          <FooterGlyph name="thumb-down" />
        </button>
        <button
          type="button"
          aria-label={copied ? 'Answer copied' : 'Copy answer'}
          title={copied ? 'Copied' : 'Copy'}
          onClick={() => void onCopy()}
          className={cn(BTN, copied && 'text-ok')}
        >
          <FooterGlyph name={copied ? 'check' : 'copy'} />
        </button>
      </div>
    </footer>
  );
}
