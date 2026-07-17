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
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { ChatModelInfo, KnowledgeMode } from '@/api';
import { Icon } from '@/ui';
import { useMentionSuggestions } from '../model/queries';
import { mentionQueryOf, type PinnedDocument } from '../model/composerHelpers';
import { ModelPicker } from './ModelPicker';
import {
  KnowledgeModeControl,
  type ModeAvailability,
} from './KnowledgeModeControl';

export type { PinnedDocument } from '../model/composerHelpers';

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
  /** Documents pinned into the next answer's retrieval scope (spec 0007 #432). */
  pinnedDocuments?: readonly PinnedDocument[];
  /** Pin a document chosen from the @-mention picker. */
  onPinDocument?: (doc: PinnedDocument) => void;
  /** Remove a pinned document (its pill's ×). */
  onUnpinDocument?: (id: string) => void;
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
  pinnedDocuments = [],
  onPinDocument,
  onUnpinDocument,
}: ComposerProps) {
  const [draft, setDraft] = useState('');
  // Bash-style history recall (spec 0006 AC-5): the index into historyEntries
  // currently shown (null = not navigating), plus the stashed in-progress draft
  // restored when navigating back past the newest entry (or on Escape).
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  const stashRef = useRef('');
  const canSend = draft.trim().length > 0 && !busy && !disabled;

  // @-mention document picker (spec 0007 #432, AC-4): active while the draft
  // ends in an @token; suggestions come from the permission-trimmed suggest
  // surface, filtered to documents and to not-already-pinned ids.
  const mentionQuery = onPinDocument ? mentionQueryOf(draft) : null;
  const [mentionDismissed, setMentionDismissed] = useState(false);
  const mentionActive = mentionQuery !== null && !mentionDismissed;
  const [mentionIndex, setMentionIndex] = useState(0);
  const suggestions = useMentionSuggestions(mentionActive ? mentionQuery : '');
  const mentionOptions = useMemo(() => {
    if (!mentionActive) return [];
    const pinned = new Set(pinnedDocuments.map((d) => d.id));
    const seen = new Set<string>();
    const out: PinnedDocument[] = [];
    for (const s of suggestions.data?.suggestions ?? []) {
      if (s.kind !== 'document' || !s.document_id) continue;
      if (pinned.has(s.document_id) || seen.has(s.document_id)) continue;
      seen.add(s.document_id);
      out.push({ id: s.document_id, name: s.text });
    }
    return out;
  }, [mentionActive, suggestions.data, pinnedDocuments]);

  // Keep the highlighted row valid as options change; re-arm dismissal per token.
  useEffect(() => {
    setMentionIndex(0);
  }, [mentionQuery]);
  useEffect(() => {
    if (mentionQuery === null) setMentionDismissed(false);
  }, [mentionQuery]);

  function pinMention(option: PinnedDocument) {
    if (!onPinDocument) return;
    onPinDocument(option);
    // Replace the @token (query included) with nothing — the pill carries it.
    setDraft((current) => current.replace(/(^|\s)@[^\s@]*$/, '$1').trimEnd());
    resetHistoryNav();
  }

  // The ghost renders ONLY over an empty, enabled composer — so it can never
  // cover or clobber user text (spec 0006 AC-4). Escape dismisses it until a
  // NEW suggestion arrives (research pass).
  const [ghostDismissed, setGhostDismissed] = useState(false);
  useEffect(() => {
    setGhostDismissed(false);
  }, [ghostSuggestion]);
  const ghost =
    draft === '' && !disabled && !ghostDismissed && ghostSuggestion ? ghostSuggestion : null;

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
    // IME safety (research pass): while composing (CJK etc.), every key —
    // including Enter and arrows — belongs to the composition, never to send,
    // recall, ghost, or the mention picker.
    if (e.nativeEvent.isComposing) return;
    // The @-mention dropdown owns its keys WHENEVER active (#434 review,
    // finding 4) — including the loading/empty states, so Enter can never send
    // the literal "@query" and arrows can never recall history under the open
    // picker. Enter with no option to pick dismisses instead of sending;
    // Escape always dismisses; Tab closes without trapping focus (APG).
    if (mentionActive) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (mentionOptions.length > 0) {
          setMentionIndex((i) =>
            e.key === 'ArrowDown' ? Math.min(i + 1, mentionOptions.length - 1) : Math.max(i - 1, 0),
          );
        }
        return;
      }
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const option = mentionOptions[Math.min(mentionIndex, mentionOptions.length - 1)];
        if (option) {
          pinMention(option);
        } else {
          setMentionDismissed(true);
        }
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMentionDismissed(true);
        return;
      }
      if (e.key === 'Tab') {
        // Close the picker and let focus move on — never a keyboard trap.
        setMentionDismissed(true);
        return;
      }
    }
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
    if (e.key === 'Escape' && ghost) {
      // Dismiss the ghost without other side effects (research pass — Copilot
      // convention); it returns only with the NEXT suggestion.
      e.preventDefault();
      setGhostDismissed(true);
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
        {/* Pinned-document pills (spec 0007 #432): sticky retrieval scope for
            subsequent sends; each × unpins. */}
        {pinnedDocuments.length > 0 && (
          <span className="lc-pins" role="group" aria-label="Pinned documents">
            {pinnedDocuments.map((doc) => (
              <span key={doc.id} className="lc-pin" title={doc.name}>
                <Icon name="at-sign" />
                <span className="lc-pin__name">{doc.name}</span>
                {onUnpinDocument && (
                  <button
                    type="button"
                    className="lc-pin__remove"
                    aria-label={`Unpin ${doc.name}`}
                    onClick={() => onUnpinDocument(doc.id)}
                  >
                    <Icon name="x" />
                  </button>
                )}
              </span>
            ))}
          </span>
        )}
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
            // Mention-picker combobox wiring (W3C APG; spec 0007 AC-4): the
            // textarea keeps DOM focus while aria-activedescendant tracks the
            // highlighted option. Attributes present only while the picker is
            // relevant so ordinary typing is unaffected.
            aria-autocomplete={onPinDocument ? 'list' : undefined}
            aria-expanded={onPinDocument ? mentionActive : undefined}
            aria-controls={mentionActive ? 'lc-mention-listbox' : undefined}
            aria-activedescendant={
              mentionActive && mentionOptions.length > 0
                ? `lc-mention-opt-${Math.min(mentionIndex, mentionOptions.length - 1)}`
                : undefined
            }
          />
          {/* @-mention document picker (spec 0007 AC-4): a listbox above the
              input while the draft ends in an @token. Options are permission-
              trimmed by the suggest surface; failure degrades to "no matches". */}
          {mentionActive && (
            <div
              className="lc-mention"
              id="lc-mention-listbox"
              role="listbox"
              aria-label="Pin a document"
            >
              {suggestions.isLoading && mentionOptions.length === 0 ? (
                <p className="lc-mention__status">Searching documents…</p>
              ) : mentionOptions.length === 0 ? (
                <p className="lc-mention__status">
                  {mentionQuery ? 'No matching documents.' : 'Type to search your documents.'}
                </p>
              ) : (
                mentionOptions.map((option, index) => (
                  <button
                    key={option.id}
                    id={`lc-mention-opt-${index}`}
                    type="button"
                    role="option"
                    aria-selected={index === mentionIndex}
                    className={`lc-mention__opt${index === mentionIndex ? ' lc-mention__opt--active' : ''}`}
                    onMouseEnter={() => setMentionIndex(index)}
                    onClick={() => pinMention(option)}
                  >
                    <Icon name="file-text" />
                    <span className="lc-mention__name">{option.name}</span>
                  </button>
                ))
              )}
            </div>
          )}
          {/* Ghost prefill (spec 0006 AC-4): visually inside the input, but
              inert — the textarea stays fully typable beneath it. The accept
              hint is the one clickable piece. */}
          {ghost && (
            <div className="lc-ghost" aria-hidden={false}>
              {/* SR affordance for the phantom text (research pass — Smart
                  Compose convention): announce once, politely. */}
              <span className="sr-only" role="status">
                Suggested question: {ghost}. Press Tab to use it, Escape to dismiss.
              </span>
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
