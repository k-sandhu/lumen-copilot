/**
 * Suggested follow-up questions (spec 0006 #429, AC-3): clickable chips under
 * the latest settled answer. Clicking sends the suggestion verbatim as the next
 * user message. Transient by design — they exist only in stream/UI state and
 * are gone after a reload (the composer always works without them).
 */
import { Icon } from '@/ui';

export interface SuggestionChipsProps {
  suggestions: string[];
  /** True while a send/stream is in flight — chips render but cannot fire. */
  disabled?: boolean;
  onPick: (text: string) => void;
}

export function SuggestionChips({ suggestions, disabled = false, onPick }: SuggestionChipsProps) {
  if (suggestions.length === 0) return null;
  return (
    <div className="lc-suggestions" role="group" aria-label="Suggested follow-up questions">
      {suggestions.map((text) => (
        <button
          key={text}
          type="button"
          className="lc-suggestion-chip"
          disabled={disabled}
          onClick={() => onPick(text)}
        >
          <Icon name="sparkles" />
          <span>{text}</span>
        </button>
      ))}
    </div>
  );
}
