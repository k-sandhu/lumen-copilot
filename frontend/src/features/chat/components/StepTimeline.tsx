/**
 * Live run-phase timeline (spec 0006 #429, AC-1): renders the stream's `step`
 * events — prepare → think → finalize → suggest — as a compact vertical list
 * while the assistant works. Dumb by design: keys are open-ended and labels are
 * server-supplied, so new backend phases (sub-agents, memory) render with zero
 * frontend changes. Transient state: the parent stops rendering it once answer
 * text flows (the answer itself is then the progress signal).
 *
 * A running step pulses (honoring prefers-reduced-motion via the shared
 * animate-pulse rule); a completed one shows a check. The list is a polite live
 * region so a screen reader hears phase changes without token spam.
 */
import { Icon } from '@/ui';
import type { ChatStep } from '@/api';

export interface StepTimelineProps {
  steps: ChatStep[];
}

function stepText(step: ChatStep): string {
  const turn = step.key === 'think' && step.turn && step.turn > 1 ? ` (turn ${step.turn})` : '';
  const detail = step.detail ? ` — ${step.detail}` : '';
  return `${step.label}${turn}${detail}`;
}

export function StepTimeline({ steps }: StepTimelineProps) {
  if (steps.length === 0) return null;
  return (
    <ol className="lc-steps" aria-label="Answer progress" aria-live="polite" aria-atomic={false}>
      {steps.map((step) => {
        const running = step.state === 'started';
        return (
          <li
            key={step.key}
            className={`lc-step ${running ? 'lc-step--running' : 'lc-step--done'}`}
          >
            {running ? (
              <span className="lc-step__dot animate-pulse" aria-hidden="true" />
            ) : (
              <span className="lc-step__check" aria-hidden="true">
                <Icon name="check" />
              </span>
            )}
            <span className="lc-step__label">{stepText(step)}</span>
          </li>
        );
      })}
    </ol>
  );
}
