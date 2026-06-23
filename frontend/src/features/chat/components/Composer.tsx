/**
 * The pinned chat composer (AC-1): a growing textarea + the model picker +
 * send/stop. Enter sends, Shift+Enter inserts a newline. While an answer is
 * streaming, Send becomes Stop (cancellable streams — frontend/AGENTS.md). The
 * input is local UI state (draft), so it lives here, not in a server cache.
 *
 * Trust-signal re-skin (#89): a knowledge-mode chip group surfaces WHAT the next
 * answer is grounded on (the wireframe composer), defaulting to "Company
 * sources" — the existing behavior (retrieve across all permitted docs).
 */
import { useState, type FormEvent, type KeyboardEvent } from 'react';
import type { ChatModelInfo } from '@/api';
import { ModelPicker } from './ModelPicker';
import { KnowledgeModeChips, type KnowledgeMode } from './KnowledgeModeChips';

export interface ComposerProps {
  models: ChatModelInfo[];
  /** Selected model id (the per-turn / session model). */
  model: string;
  onModelChange: (modelId: string) => void;
  /** True while a send request is in flight or the answer is streaming. */
  busy: boolean;
  /** True while the answer is streaming (shows Stop instead of Send). */
  streaming: boolean;
  onSend: (content: string) => void;
  onStop: () => void;
  /** Disable everything (e.g. no session selected). */
  disabled?: boolean;
}

export function Composer({
  models,
  model,
  onModelChange,
  busy,
  streaming,
  onSend,
  onStop,
  disabled = false,
}: ComposerProps) {
  const [draft, setDraft] = useState('');
  // Knowledge mode is a presentational scope indicator (#89). It defaults to
  // "company" (all permitted docs — the existing behavior); "selected" stays
  // disabled until a collection scope is wired, so we never imply a fake feature.
  const [knowledgeMode, setKnowledgeMode] = useState<KnowledgeMode>('company');
  const canSend = draft.trim().length > 0 && !busy && !disabled;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSend) return;
    onSend(draft.trim());
    setDraft('');
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (canSend) {
        onSend(draft.trim());
        setDraft('');
      }
    }
  }

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={submit}
      aria-label="Message composer"
    >
      <KnowledgeModeChips
        value={knowledgeMode}
        onChange={setKnowledgeMode}
        disabled={disabled || streaming}
      />
      <div className="flex items-end gap-2">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          rows={1}
          placeholder={disabled ? 'Start or pick a chat to begin' : 'Ask about your documents…'}
          aria-label="Message"
          className="max-h-40 min-h-[2.5rem] min-w-0 flex-1 resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground placeholder:text-foreground-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        />
        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            className="shrink-0 rounded-md border border-border bg-surface px-3 py-2 text-sm font-medium text-foreground hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!canSend}
            className="shrink-0 rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {busy ? 'Sending…' : 'Send'}
          </button>
        )}
      </div>
      <div className="flex items-center justify-between">
        <ModelPicker
          models={models}
          value={model}
          onChange={onModelChange}
          disabled={disabled || streaming}
        />
        <span className="text-[11px] text-foreground-muted">
          Enter to send · Shift+Enter for a new line
        </span>
      </div>
    </form>
  );
}
