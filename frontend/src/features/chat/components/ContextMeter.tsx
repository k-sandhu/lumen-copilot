/**
 * The conversation context meter (spec 0007 #432, AC-1 — Cursor-style): how
 * full the model's input window was last turn, plus session token totals in the
 * tooltip. Reads GET /chat/sessions/{id}/usage — the assembler's own budget
 * arithmetic — and is invalidated on stream `done`, so it moves as answers
 * settle. A fresh session renders an honest empty meter; a failed read renders
 * nothing (the meter is a nicety, never a blocker).
 */
import { useSessionUsage } from '../model/queries';
import { formatTokens } from '../model/composerHelpers';

export interface ContextMeterProps {
  sessionId: string;
}


export function ContextMeter({ sessionId }: ContextMeterProps) {
  const usage = useSessionUsage(sessionId);
  // Silent on loading/error AND on a malformed payload — the meter is a
  // nicety; it must never take the conversation down with it.
  if (usage.isLoading || usage.isError || !usage.data?.totals) return null;

  const { totals, last, input_budget_tokens: budget, window_known: known } = usage.data;
  const lastPrompt = last?.prompt_tokens ?? 0;
  const percent = Math.min(100, Math.round((lastPrompt / Math.max(1, budget)) * 100));
  const label =
    totals.answers === 0
      ? `Context ${formatTokens(budget)} available`
      : `Context ${percent}% · ${formatTokens(lastPrompt)}/${formatTokens(budget)}`;
  const title =
    `This conversation: ${totals.answers} answer${totals.answers === 1 ? '' : 's'}, ` +
    `${formatTokens(totals.total_tokens)} tokens total ` +
    `(${formatTokens(totals.prompt_tokens)} in / ${formatTokens(totals.completion_tokens)} out, ` +
    `${formatTokens(totals.cached_prompt_tokens)} cached). ` +
    `Last turn used ${formatTokens(lastPrompt)} of the ${formatTokens(budget)}-token input budget` +
    (known ? '.' : ' (conservative estimate — unknown model window).');

  return (
    <span
      className="lc-ctx-meter"
      role="status"
      aria-label={`Context usage: ${label}`}
      title={title}
    >
      <span className="lc-ctx-meter__bar" aria-hidden="true">
        <span className="lc-ctx-meter__fill" style={{ width: `${percent}%` }} />
      </span>
      <span className="lc-ctx-meter__label">
        {label}
        {!known && <span aria-hidden="true">*</span>}
      </span>
    </span>
  );
}
