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
import { Icon } from '@/ui';
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
  /** The caller's saved default model id (spec 0005); null when unset. */
  defaultModelId?: string | null;
  /** Persist the currently-selected model as the caller's default (#144). */
  onSetDefaultModel?: () => void;
  /** True while the default-model preference write is in flight. */
  settingDefault?: boolean;
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
  defaultModelId,
  onSetDefaultModel,
  settingDefault = false,
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
    <form className="lc-composer" onSubmit={submit} aria-label="Message composer">
      <div className="lc-composer__chips">
        <KnowledgeModeChips
          value={knowledgeMode}
          onChange={setKnowledgeMode}
          disabled={disabled || streaming}
        />
      </div>

      <div className="lc-composer__box">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          rows={1}
          placeholder={
            disabled
              ? 'Start or pick a chat to begin'
              : 'Ask anything about your work…  (Enter to send, Shift+Enter for a new line)'
          }
          aria-label="Message"
          className="lc-composer__input"
        />
        <div className="lc-composer__bar">
          <ModelPicker
            models={models}
            value={model}
            onChange={onModelChange}
            disabled={disabled || streaming}
          />
          {model && onSetDefaultModel ? (
            model === defaultModelId ? (
              <span
                className="lc-default-tag"
                title="This is your default model for new chats"
              >
                <Icon name="check" />
                Default
              </span>
            ) : (
              <button
                type="button"
                className="lc-default-btn"
                onClick={onSetDefaultModel}
                disabled={settingDefault || disabled}
                title="Use this model as the default for new chats"
              >
                {settingDefault ? 'Saving…' : 'Set as default'}
              </button>
            )
          ) : null}
          <div className="lc-composer__bar-spacer" />
          {streaming ? (
            <button type="button" onClick={onStop} aria-label="Stop generating" className="lc-stop-btn">
              <span className="lc-stop-glyph" aria-hidden="true" />
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!canSend}
              aria-label={busy ? 'Sending message' : 'Send message'}
              className="lc-send-btn"
            >
              <Icon name="send" />
            </button>
          )}
        </div>
      </div>

      <p className="lc-composer__hint">
        Lumen can be wrong — every answer is grounded and cited so you can verify.
      </p>
    </form>
  );
}
