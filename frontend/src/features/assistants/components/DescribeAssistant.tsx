/**
 * DescribeAssistant (#213, E6-1) — the conversational agent builder entry:
 * "Describe your assistant". The user types a plain-language description; on submit
 * it calls POST /assistants/draft (which creates NOTHING) and, on success, swaps to
 * the existing AssistantEditor pre-filled with the drafted config plus the builder's
 * clarifications / notes / warnings shown above the form. The user reviews, edits,
 * and saves — the assistant is only created when they save the pre-filled editor.
 *
 * Implements every async state (frontend/AGENTS.md "every state, not just success"):
 *   idle    → the description textarea + "Draft it" CTA
 *   pending → the CTA shows "Drafting…" and the textarea is disabled
 *   error   → an inline, actionable message (401 messaged distinctly, INV-4); a 422
 *             (blank/oversize) is prevented client-side and messaged if it slips
 *   success → the pre-filled editor (with advisories) replaces this form
 *
 * A11y: a labelled textarea with a describedby error wiring, managed focus back to
 * the textarea on a failed draft, and a keyboard-submittable form.
 */
import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '@/api';
import type { AssistantDraft } from '@/api';
import { Icon } from '@/ui';
import { useDraftAssistant } from '../model/queries';
import { formFromDraft } from '../model/form';
import { AssistantEditor } from './AssistantEditor';

const MAX_DESCRIPTION = 4000;

export function DescribeAssistant() {
  const draftMutation = useDraftAssistant();
  const [description, setDescription] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AssistantDraft | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // On a successful draft, hand off to the editor pre-filled with the draft +
  // advisories. Nothing is created until the user saves there.
  if (result) {
    return (
      <AssistantEditor
        assistantId={null}
        initialForm={formFromDraft(result.draft)}
        advisory={{
          clarifications: result.clarifications ?? [],
          notes: result.notes ?? [],
          warnings: result.warnings ?? [],
        }}
      />
    );
  }

  const busy = draftMutation.isPending;

  const handleSubmit = () => {
    setError(null);
    const text = description.trim();
    if (text.length === 0) {
      setError('Describe what you want the assistant to do.');
      textareaRef.current?.focus();
      return;
    }
    draftMutation.mutate(
      { description: text },
      {
        onSuccess: (draft) => setResult(draft),
        onError: (err) => {
          setError(draftErrorMessage(err));
          textareaRef.current?.focus();
        },
      },
    );
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6 px-5 py-6">
      <header className="space-y-1">
        <h1 className="flex items-center gap-2 text-base font-semibold">
          <Icon name="sparkles" className="shrink-0 text-accent" aria-hidden="true" />
          Describe your assistant
        </h1>
        <p className="text-sm text-foreground-muted">
          Tell the builder what you want in plain language. It drafts a name, instructions, the
          sources it should read, and its tools — you review and edit everything before saving.
          Nothing is created until you save.
        </p>
      </header>

      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          handleSubmit();
        }}
        noValidate
      >
        {error ? (
          <p
            role="alert"
            className="flex items-start gap-1.5 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger"
          >
            <Icon name="alert-triangle" className="mt-px shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </p>
        ) : null}

        <div>
          <label htmlFor="assistant-description-prompt" className="mb-1 block text-sm font-medium">
            What should it do?
          </label>
          <textarea
            id="assistant-description-prompt"
            ref={textareaRef}
            value={description}
            disabled={busy}
            rows={6}
            maxLength={MAX_DESCRIPTION}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? 'assistant-description-prompt-error' : undefined}
            onChange={(e) => {
              setDescription(e.target.value);
              if (error) setError(null);
            }}
            placeholder="e.g. A benefits helper that answers questions from our HR handbook and always cites the policy it used. It should only read the HR collection."
            className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
          />
          {error ? (
            <p id="assistant-description-prompt-error" className="sr-only">
              {error}
            </p>
          ) : (
            <p className="mt-1 text-xs text-foreground-muted">
              The more specific you are about sources and tools, the fewer questions the builder
              needs to ask.
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
          <button
            type="submit"
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            <Icon name="sparkles" className="shrink-0" aria-hidden="true" />
            {busy ? 'Drafting…' : 'Draft it'}
          </button>
          <Link
            to="/assistants/new"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Start from a blank form instead
          </Link>
          <Link
            to="/assistants"
            className="ml-auto rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Back to library
          </Link>
        </div>
      </form>
    </div>
  );
}

/**
 * A user-facing message for a failed draft. A 401 (expired/missing token, INV-4) is
 * a re-auth dead-end; message it distinctly. A 422 shouldn't normally reach here
 * (the client blocks a blank description), but if it does, surface the server's
 * detail. Everything else is transient.
 */
function draftErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return 'Your session expired. Sign in again to use the builder.';
    }
    if (error.problem?.detail) {
      return error.problem.detail;
    }
    return error.displayMessage || 'Could not draft an assistant. Please try again.';
  }
  return 'Could not draft an assistant. Please try again.';
}
