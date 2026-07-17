/**
 * The pinned chat composer (AC-1): a growing textarea + the model picker +
 * send/stop. Enter sends, Shift+Enter inserts a newline. While an answer is
 * streaming, Send becomes Stop (cancellable streams — frontend/AGENTS.md). The
 * input is local UI state (draft), so it lives here, not in a server cache.
 *
 * Trust-signal re-skin (#89): a knowledge-mode control surfaces WHAT the next
 * answer may draw on (the wireframe composer). #221 (epic E3-12) evolves it into
 * the four wire modes (company / uploaded / web / model), reflecting the active
 * assistant's `knowledgeScope.modes` with a per-chat override, and rendering the
 * governed WEB toggle disabled-with-a-reason when it isn't enabled.
 *
 * Spec 0006 (#429) affordances:
 * - **Ghost prefill (AC-4).** When the composer is empty, the top suggested
 *   follow-up renders as ghost text with an explicit accept affordance (Tab, or
 *   clicking the hint). Typing anything dismisses it — the ghost can never
 *   overwrite or race user input (it renders only while the draft is empty).
 * - **History recall (AC-5).** ArrowUp with the caret on the FIRST line walks
 *   the caller's own previous messages (newest first), bash-style; ArrowDown on
 *   the LAST line walks back and finally restores the stashed draft. Escape
 *   restores the draft immediately. Editing a recalled entry ends navigation
 *   (the edit becomes the new draft, as in a shell).
 */
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { ChatModelInfo, KnowledgeMode } from '@/api';
import { Icon } from '@/ui';
import { ModelPicker } from './ModelPicker';
import {
  KnowledgeModeControl,
  type ModeAvailability,
} from './KnowledgeModeControl';

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
  /** The active knowledge modes for the next turn (#221). */
  modes: readonly KnowledgeMode[];
  onModesChange: (next: KnowledgeMode[]) => void;
  /** Per-mode availability (web is governed / disabled-with-reason; #221). */
  modeAvailability?: Partial<Record<KnowledgeMode, ModeAvailability>>;
  /**
   * The best next question (spec 0006 #429, AC-4) — ghost-rendered in the EMPTY
   * composer; Tab (or clicking the hint) accepts it into the draft. Null/absent
   * ⇒ no ghost.
   */
  ghostSuggestion?: string | null;
  /**
   * The caller's own previous messages, newest first (spec 0006 #429, AC-5) —
   * recalled bash-style with ArrowUp/ArrowDown. Scoped to the current
   * conversation by the parent.
   */
  historyEntries?: readonly string[];
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
  modes,
  onModesChange,
  modeAvailability,
  ghostSuggestion = null,
  historyEntries = [],
}: ComposerProps) {
  const [draft, setDraft] = useState('');
  // Bash-style history recall (spec 0006 AC-5): the index into historyEntries
  // currently shown (null = not navigating), plus the stashed in-progress draft
  // restored when navigating back past the newest entry (or on Escape).
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  const stashRef = useRef('');
  const canSend = draft.trim().length > 0 && !busy && !disabled;
  // The ghost renders ONLY over an empty, enabled composer — so it can never
  // cover or clobber user text (spec 0006 AC-4).
  const ghost = draft === '' && !disabled && ghostSuggestion ? ghostSuggestion : null;

  // A new conversation's history invalidates any in-flight navigation.
  useEffect(() => {
    setHistoryIndex(null);
    stashRef.current = '';
  }, [historyEntries]);

  function resetHistoryNav() {
    setHistoryIndex(null);
    stashRef.current = '';
  }

  function sendDraft() {
    onSend(draft.trim());
    setDraft('');
    resetHistoryNav();
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSend) return;
    sendDraft();
  }

  function acceptGhost() {
    if (!ghost) return;
    setDraft(ghost);
    resetHistoryNav();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (canSend) sendDraft();
      return;
    }
    if (e.key === 'Tab' && !e.shiftKey && ghost) {
      // Accept the ghost suggestion (spec 0006 AC-4). Only intercepts Tab while
      // a ghost is visible — normal focus traversal is otherwise untouched.
      e.preventDefault();
      acceptGhost();
      return;
    }
    const el = e.currentTarget;
    const collapsed = el.selectionStart === el.selectionEnd;
    if (e.key === 'ArrowUp' && collapsed && historyEntries.length > 0) {
      // Only from the first line (multiline-safe, spec 0006 AC-5): within a
      // taller draft, ArrowUp stays ordinary caret movement.
      const onFirstLine = !el.value.slice(0, el.selectionStart).includes('\n');
      if (!onFirstLine) return;
      const next = historyIndex === null ? 0 : Math.min(historyIndex + 1, historyEntries.length - 1);
      if (historyIndex === null) stashRef.current = draft;
      if (next === historyIndex) return; // already at the oldest entry
      e.preventDefault();
      setHistoryIndex(next);
      setDraft(historyEntries[next] ?? '');
      return;
    }
    if (e.key === 'ArrowDown' && collapsed && historyIndex !== null) {
      const onLastLine = !el.value.slice(el.selectionEnd).includes('\n');
      if (!onLastLine) return;
      e.preventDefault();
      if (historyIndex === 0) {
        // Past the newest entry: restore the stashed draft (bash behavior).
        setDraft(stashRef.current);
        resetHistoryNav();
      } else {
        const next = historyIndex - 1;
        setHistoryIndex(next);
        setDraft(historyEntries[next] ?? '');
      }
      return;
    }
    if (e.key === 'Escape' && historyIndex !== null) {
      e.preventDefault();
      setDraft(stashRef.current);
      resetHistoryNav();
    }
  }

  function onDraftChange(next: string) {
    setDraft(next);
    // Editing ends history navigation — the edit is the new draft (as in a
    // shell); the stash is spent.
    if (historyIndex !== null) resetHistoryNav();
  }

  return (
    <form className="lc-composer" onSubmit={submit} aria-label="Message composer">
      <div className="lc-composer__chips">
        <KnowledgeModeControl
          value={modes}
          onChange={onModesChange}
          availability={modeAvailability}
          disabled={disabled || streaming}
        />
      </div>

      <div className="lc-composer__box">
        <div className="lc-composer__inputwrap">
          <textarea
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={disabled}
            rows={1}
            placeholder={
              disabled
                ? 'Start or pick a chat to begin'
                : ghost
                  ? undefined
                  : 'Ask anything about your work…  (Enter to send, Shift+Enter for a new line)'
            }
            aria-label="Message"
            className="lc-composer__input"
          />
          {/* Ghost prefill (spec 0006 AC-4): visually inside the input, but
              inert — the textarea stays fully typable beneath it. The accept
              hint is the one clickable piece. */}
          {ghost && (
            <div className="lc-ghost" aria-hidden={false}>
              <span className="lc-ghost__text" aria-hidden="true">
                {ghost}
              </span>
              <button
                type="button"
                className="lc-ghost__accept"
                onClick={acceptGhost}
                aria-label={`Use suggested question: ${ghost}`}
                title="Use this suggestion (Tab)"
                tabIndex={-1}
              >
                Tab ⇥
              </button>
            </div>
          )}
        </div>
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
