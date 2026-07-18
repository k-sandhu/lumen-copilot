/**
 * Clickable options for a clarifying question (spec 0006 #429, AC-2): rendered
 * under the assistant turn that asked (live from `event:ask_user`, reloaded from
 * `Message.question`). Clicking sends the option label verbatim as the next user
 * message — an honest, visible transcript, no hidden protocol.
 *
 * Active ONLY while the question is the conversation's last message and nothing
 * is in flight; an answered/older question renders inert with the chosen option
 * highlighted (derived from the following user turn). Free-text stays available
 * through the composer — these buttons are an accelerator, not a gate.
 */
import type { AskUserQuestion } from '@/api';

export interface AskUserOptionsProps {
  question: AskUserQuestion;
  /** False once answered / superseded / a stream is in flight — renders inert. */
  active: boolean;
  /** The following user turn's text, to highlight which option was chosen. */
  chosenAnswer?: string | undefined;
  /** Send the clicked option label as the next user message. */
  onChoose?: ((label: string) => void) | undefined;
}

export function AskUserOptions({ question, active, chosenAnswer, onChoose }: AskUserOptionsProps) {
  if (question.options.length === 0) return null;
  const chosenKey = chosenAnswer?.trim().toLocaleLowerCase();
  return (
    <div className="lc-askuser" role="group" aria-label="Answer options">
      {question.options.map((option) => {
        const chosen =
          chosenKey !== undefined && option.label.trim().toLocaleLowerCase() === chosenKey;
        return (
          <button
            key={option.label}
            type="button"
            className={`lc-askuser__opt${chosen ? ' lc-askuser__opt--chosen' : ''}`}
            disabled={!active}
            aria-pressed={chosen}
            onClick={active && onChoose ? () => onChoose(option.label) : undefined}
          >
            <span className="lc-askuser__label">{option.label}</span>
            {option.description ? (
              <span className="lc-askuser__desc">{option.description}</span>
            ) : null}
          </button>
        );
      })}
      {active && question.allow_free_text ? (
        <p className="lc-askuser__hint">…or type your own answer below.</p>
      ) : null}
    </div>
  );
}
