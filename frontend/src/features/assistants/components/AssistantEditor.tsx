/**
 * AssistantEditor (#212, AC-1) — the builder form for `/assistants/new` (create)
 * and `/assistants/{id}` (edit). One form drives create→edit→publish:
 *
 *   • New:  fill the form → "Save draft" POSTs /assistants → navigate to the
 *           `/assistants/{id}` editor (now in edit mode, owner assignable).
 *   • Edit: the head loads (loading / error+retry / success) → change fields →
 *           "Save" PATCHes the head → "Publish" freezes a version (draft →
 *           published), disabled until owner + distinct backup owner are set.
 *
 * Validation (422 / INV-8) surfaces INLINE: a name error under the name field,
 * field-level problem errors mapped where possible, and a general publish/save
 * error banner. A 404/403 on load renders an error state with retry, never a crash
 * (AC-5). Every async surface (detail, member roster, scope lists) has its own
 * loading/empty/error state — no blank panes (AC-3).
 *
 * A11y: labelled inputs, a describedby error wiring on the name field, managed
 * focus to the first invalid field on a failed save; keyboard-navigable throughout.
 */
import { useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError } from '@/api';
import type { Assistant, ChatModelInfo, ProblemFieldError } from '@/api';
import { Icon } from '@/ui';
import { ModelPicker } from '@/features/chat';
import {
  useAssistant,
  useCreateAssistant,
  useMembers,
  useModels,
  usePublishAssistant,
  useUpdateAssistant,
} from '../model/queries';
import {
  AUTONOMY_OPTIONS,
  STATUS_LABEL,
} from '../model/presentation';
import {
  canPublish,
  emptyForm,
  formFromAssistant,
  toCreateBody,
  toUpdateBody,
  type AssistantFormState,
} from '../model/form';
import { KnowledgeScopePicker } from './KnowledgeScopePicker';
import { ToolAllowlist } from './ToolAllowlist';
import { OwnerPickers } from './OwnerPickers';
import { VersionHistory } from './VersionHistory';
import { TestPanel } from './TestPanel';

interface AssistantEditorProps {
  /** The assistant id in edit mode; null for `/assistants/new`. */
  assistantId: string | null;
}

export function AssistantEditor({ assistantId }: AssistantEditorProps) {
  const detail = useAssistant(assistantId);

  // In new mode there is nothing to load. In edit mode, gate the form on the
  // head resolving — a 404/403 renders an error state with retry (AC-5).
  if (assistantId !== null) {
    if (detail.isPending) return <EditorSkeleton />;
    if (detail.isError) {
      return <LoadError error={detail.error} onRetry={() => void detail.refetch()} />;
    }
  }

  return (
    <EditorForm
      key={assistantId ?? 'new'}
      assistantId={assistantId}
      initial={detail.data ? formFromAssistant(detail.data) : emptyForm()}
      current={detail.data ?? null}
    />
  );
}

function EditorForm({
  assistantId,
  initial,
  current,
}: {
  assistantId: string | null;
  initial: AssistantFormState;
  current: Assistant | null;
}) {
  const navigate = useNavigate();
  const models = useModels();
  const members = useMembers();

  const create = useCreateAssistant();
  const update = useUpdateAssistant(assistantId ?? '');
  const publish = usePublishAssistant(assistantId ?? '');

  const [form, setForm] = useState<AssistantFormState>(initial);
  const [nameError, setNameError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  const set = <K extends keyof AssistantFormState>(key: K, value: AssistantFormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setSaved(false);
  };

  const busy = create.isPending || update.isPending || publish.isPending;

  const validateName = (): boolean => {
    if (form.name.trim().length === 0) {
      setNameError('Give your assistant a name.');
      nameRef.current?.focus();
      return false;
    }
    setNameError(null);
    return true;
  };

  const applyProblem = (error: unknown) => {
    if (error instanceof ApiError && error.problem) {
      const fieldErrors = error.problem.errors ?? [];
      const nameFieldErr = fieldErrors.find((e: ProblemFieldError) => e.field === 'name');
      if (nameFieldErr) {
        setNameError(nameFieldErr.message);
        nameRef.current?.focus();
      }
      setFormError(error.displayMessage);
    } else {
      setFormError('Something went wrong. Please try again.');
    }
  };

  const handleSave = () => {
    setFormError(null);
    if (!validateName()) return;

    if (assistantId === null) {
      // Create → land on the new assistant's editor so owner/publish become
      // available (owner is server-assigned; edit mode can then set the backup).
      create.mutate(toCreateBody(form), {
        onSuccess: (a) => navigate(`/assistants/${a.id}`),
        onError: applyProblem,
      });
    } else {
      update.mutate(toUpdateBody(form), {
        onSuccess: () => setSaved(true),
        onError: applyProblem,
      });
    }
  };

  const handlePublish = () => {
    if (assistantId === null) return;
    setFormError(null);
    if (!validateName()) return;
    // Persist the head first so the published version freezes the latest edits,
    // then publish. Both surface a 422 inline (missing backup owner, INV-8).
    update.mutate(toUpdateBody(form), {
      onSuccess: () =>
        publish.mutate(
          {},
          {
            onSuccess: () => setSaved(true),
            onError: applyProblem,
          },
        ),
      onError: applyProblem,
    });
  };

  const publishable = canPublish(form);
  const isNew = assistantId === null;

  return (
    <div className="mx-auto max-w-3xl space-y-8 px-5 py-6">
    <form
      className="space-y-6"
      onSubmit={(e) => {
        e.preventDefault();
        handleSave();
      }}
      noValidate
    >
      <EditorHeader current={current} isNew={isNew} />

      {formError ? (
        <p
          role="alert"
          className="flex items-start gap-1.5 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger"
        >
          <Icon name="alert-triangle" className="mt-px shrink-0" />
          <span>{formError}</span>
        </p>
      ) : null}
      {saved ? (
        <p role="status" className="rounded-md border border-ok/40 bg-ok/10 p-3 text-sm text-ok">
          Saved.
        </p>
      ) : null}

      {/* --- Identity --- */}
      <section className="space-y-4">
        <Field label="Name" htmlFor="assistant-name" required error={nameError}>
          <input
            id="assistant-name"
            ref={nameRef}
            type="text"
            value={form.name}
            maxLength={200}
            disabled={busy}
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? 'assistant-name-error' : undefined}
            onChange={(e) => {
              set('name', e.target.value);
              if (nameError) setNameError(null);
            }}
            placeholder="e.g. Benefits helper"
            className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
          />
        </Field>

        <Field label="Description" htmlFor="assistant-description">
          <input
            id="assistant-description"
            type="text"
            value={form.description}
            disabled={busy}
            onChange={(e) => set('description', e.target.value)}
            placeholder="A one-line summary shown on the assistant card."
            className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          />
        </Field>

        <Field
          label="Instructions"
          htmlFor="assistant-instructions"
          hint="Plain-language guidance injected at run time (persona / role). Grounding and citation rules are always enforced."
        >
          <textarea
            id="assistant-instructions"
            value={form.instructions}
            disabled={busy}
            rows={5}
            onChange={(e) => set('instructions', e.target.value)}
            placeholder="You are a friendly benefits assistant. Answer from the HR handbook and cite the policy you used…"
            className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          />
        </Field>
      </section>

      {/* --- Model --- */}
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Model</h2>
        <ModelSelect
          models={models.data?.items}
          isPending={models.isPending}
          isError={models.isError}
          value={form.model}
          disabled={busy}
          onChange={(id) => set('model', id)}
        />
      </section>

      {/* --- Knowledge scope --- */}
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Knowledge</h2>
        <KnowledgeScopePicker
          value={form.knowledgeScope}
          disabled={busy}
          onChange={(scope) => set('knowledgeScope', scope)}
        />
      </section>

      {/* --- Tools --- */}
      <section>
        <ToolAllowlist
          value={form.toolAllowlist}
          disabled={busy}
          onChange={(tools) => set('toolAllowlist', tools)}
        />
      </section>

      {/* --- Autonomy --- */}
      <section className="space-y-2">
        <Field label="Autonomy" htmlFor="assistant-autonomy" hint={autonomyHint(form)}>
          <select
            id="assistant-autonomy"
            value={form.autonomyLevel}
            disabled={busy}
            onChange={(e) => set('autonomyLevel', e.target.value as AssistantFormState['autonomyLevel'])}
            className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            {AUTONOMY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
      </section>

      {/* --- Ownership --- */}
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Ownership</h2>
        {isNew ? (
          <p className="rounded-md border border-border bg-surface-muted p-3 text-xs text-foreground-muted">
            You’ll be set as the owner when you save the draft. Set a backup owner then to enable
            publishing.
          </p>
        ) : (
          <OwnerPickers
            owner={form.owner || null}
            backupOwner={form.backupOwner || null}
            onOwnerChange={(id) => set('owner', id ?? '')}
            onBackupChange={(id) => set('backupOwner', id ?? '')}
            members={members.data?.items}
            isPending={members.isPending}
            isError={members.isError}
            error={members.error}
            onRetry={() => void members.refetch()}
            disabled={busy}
          />
        )}
      </section>

      {/* --- Actions --- */}
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
        <button
          type="submit"
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name="check" className="shrink-0" />
          {isNew ? (create.isPending ? 'Creating…' : 'Save draft') : update.isPending ? 'Saving…' : 'Save'}
        </button>

        {!isNew ? (
          <button
            type="button"
            onClick={handlePublish}
            disabled={busy || !publishable}
            title={
              publishable
                ? undefined
                : 'Set an owner and a distinct backup owner to publish.'
            }
            className="inline-flex items-center gap-1.5 rounded-md border border-accent px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            <Icon name="send" className="shrink-0" />
            {publish.isPending ? 'Publishing…' : 'Publish'}
          </button>
        ) : null}

        <button
          type="button"
          onClick={() => navigate('/assistants')}
          disabled={busy}
          className="ml-auto rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          Back to library
        </button>
      </div>

      {!isNew && !publishable ? (
        <p className="text-xs text-foreground-muted">
          Publishing needs an owner and a distinct backup owner (ADR-0011 §4).
        </p>
      ) : null}
    </form>

    {/* Test/preview/debug (#215) — edit mode only; a draft with no id cannot be
        tested. Runs the working config with write-tier tools simulated/denied —
        no real side effect — and shows the debug trace. */}
    {!isNew && assistantId ? (
      <div className="border-t border-border pt-6">
        <TestPanel assistantId={assistantId} />
      </div>
    ) : null}

    {/* Version history + rollback (#214) — edit mode only; a draft with no id
        has no history to show. Publishing above appends a version here. */}
    {!isNew && current ? (
      <div className="border-t border-border pt-6">
        <VersionHistory assistant={current} members={members.data?.items} />
      </div>
    ) : null}
    </div>
  );
}

function EditorHeader({ current, isNew }: { current: Assistant | null; isNew: boolean }) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-2">
      <div>
        <h1 className="text-base font-semibold">{isNew ? 'New assistant' : 'Edit assistant'}</h1>
        <p className="text-sm text-foreground-muted">
          {isNew
            ? 'Describe what it does and pick what it can read — no code required.'
            : 'Refine the working draft, then publish a version to make it usable.'}
        </p>
      </div>
      {current ? (
        <span className="rounded-full bg-surface-muted px-2.5 py-0.5 text-xs font-medium text-foreground-muted">
          {STATUS_LABEL[current.status]}
          {current.version != null ? ` · v${current.version}` : ''}
        </span>
      ) : null}
    </header>
  );
}

function autonomyHint(form: AssistantFormState): string {
  return AUTONOMY_OPTIONS.find((o) => o.value === form.autonomyLevel)?.hint ?? '';
}

/**
 * The model picker with its own states. Reuses the chat ModelPicker (presentational)
 * and adds a "smart default" option so the assistant can defer to the server default.
 */
function ModelSelect({
  models,
  isPending,
  isError,
  value,
  disabled,
  onChange,
}: {
  models: ChatModelInfo[] | undefined;
  isPending: boolean;
  isError: boolean;
  value: string;
  disabled: boolean;
  onChange: (id: string) => void;
}) {
  if (isPending) {
    return <div role="status" aria-label="Loading models" className="lc-skeleton" style={{ width: '50%' }} />;
  }
  if (isError || !models || models.length === 0) {
    // Degrade gracefully: a free-text model id keeps the form usable even if the
    // registry is unavailable, and "smart default" is always valid.
    return (
      <div className="space-y-1">
        <p className="text-xs text-foreground-muted">
          Model registry unavailable — the assistant will use the smart server default.
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex items-center gap-2">
        <span className="text-sm text-foreground-muted">Use</span>
        <select
          aria-label="Model default"
          value={value === '' ? 'default' : 'pick'}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value === 'default' ? '' : (models[0]?.id ?? ''))}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <option value="default">Smart default</option>
          <option value="pick">A specific model</option>
        </select>
      </label>
      {value !== '' ? (
        <ModelPicker models={models} value={value} disabled={disabled} onChange={onChange} />
      ) : null}
    </div>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  required,
  error,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  required?: boolean;
  error?: string | null;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0">
      <label htmlFor={htmlFor} className="mb-1 block text-sm font-medium">
        {label}
        {required ? <span className="text-danger"> *</span> : null}
      </label>
      {children}
      {error ? (
        <p id={`${htmlFor}-error`} role="alert" className="mt-1 text-xs text-danger">
          {error}
        </p>
      ) : hint ? (
        <p className="mt-1 text-xs text-foreground-muted">{hint}</p>
      ) : null}
    </div>
  );
}

function EditorSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="mx-auto max-w-3xl space-y-4 px-5 py-6">
      <span className="sr-only">Loading assistant…</span>
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="space-y-2" aria-hidden="true">
          <div className="lc-skeleton" style={{ width: '25%' }} />
          <div className="lc-skeleton" style={{ width: '100%', height: 36 }} />
        </div>
      ))}
    </div>
  );
}

function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  const notFound = status === 404;
  const unauthorized = status === 401;
  const message = notFound
    ? 'This assistant doesn’t exist, or you don’t have access to it.'
    : unauthorized
      ? 'Your session expired. Sign in again to edit assistants.'
      : error instanceof ApiError
        ? error.displayMessage || 'Could not load this assistant.'
        : 'Could not load this assistant.';

  return (
    <div className="mx-auto max-w-3xl px-5 py-6">
      <div
        role="alert"
        className="flex flex-col items-center gap-3 rounded-xl border border-danger/40 bg-danger/10 p-8 text-center"
      >
        <Icon name="alert-triangle" aria-hidden="true" />
        <p className="text-sm font-medium text-danger">Couldn’t load assistant</p>
        <p className="max-w-sm text-sm text-foreground-muted">{message}</p>
        {!unauthorized && !notFound ? (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Retry
          </button>
        ) : null}
      </div>
    </div>
  );
}
