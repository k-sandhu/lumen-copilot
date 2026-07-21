/**
 * ScheduleEditor (#237, AC-1/AC-4) — the create/edit form for `/schedules/new`
 * and `/schedules/{id}`. Pick the assistant (GET /assistants), build the cadence
 * (cron or structured) with a validated timezone, set input params + the delivery
 * destination + overlap policy, and save.
 *
 * Validation (AC-4): cron/timezone are validated client-side with friendly
 * messages BEFORE the round-trip; a server 422 (INV-8, `invalid_cron` /
 * `invalid_timezone`) is mapped back onto the matching field so the message is
 * actionable, not a raw status. Every async surface has its own state: the head
 * load (loading / error+retry / success), the assistant roster (loading / empty /
 * error), and the save mutation (busy / error banner) — no blank panes.
 */
import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError } from '@/api';
import type { Schedule } from '@/api';
import { Icon } from '@/ui';
import {
  useAssistantsList,
  useCreateSchedule,
  useDeleteSchedule,
  useSchedule,
  useUpdateSchedule,
} from '../model/queries';
import {
  DIGEST_OPTIONS,
  OVERLAP_OPTIONS,
  emptyForm,
  formFromSchedule,
  isValid,
  toCreateBody,
  toUpdateBody,
  validateForm,
  type ScheduleFormErrors,
  type ScheduleFormState,
} from '../model/form';
import { CadenceInput } from './CadenceInput';
import { TimezoneInput } from './TimezoneInput';
import { ParamsEditor } from './ParamsEditor';
import { ErrorState, SkeletonRows } from './StateViews';

interface ScheduleEditorProps {
  /** The schedule id in edit mode; null for `/schedules/new`. */
  scheduleId: string | null;
}

export function ScheduleEditor({ scheduleId }: ScheduleEditorProps) {
  const detail = useSchedule(scheduleId);

  if (scheduleId !== null) {
    if (detail.isPending) {
      return (
        <div className="mx-auto max-w-2xl px-5 py-6">
          <SkeletonRows count={4} label="Loading schedule…" />
        </div>
      );
    }
    if (detail.isError) {
      return (
        <div className="mx-auto max-w-2xl px-5 py-6">
          <ErrorState
            title="Couldn’t load schedule"
            error={detail.error}
            onRetry={() => void detail.refetch()}
            busy={detail.isFetching}
          />
        </div>
      );
    }
  }

  return (
    <EditorForm
      key={scheduleId ?? 'new'}
      scheduleId={scheduleId}
      initial={detail.data ? formFromSchedule(detail.data) : emptyForm()}
      current={detail.data ?? null}
    />
  );
}

function EditorForm({
  scheduleId,
  initial,
  current,
}: {
  scheduleId: string | null;
  initial: ScheduleFormState;
  current: Schedule | null;
}) {
  const navigate = useNavigate();
  const assistants = useAssistantsList();
  const create = useCreateSchedule();
  const update = useUpdateSchedule(scheduleId ?? '');
  const remove = useDeleteSchedule();

  const [form, setForm] = useState<ScheduleFormState>(initial);
  const [errors, setErrors] = useState<ScheduleFormErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const assistantRef = useRef<HTMLSelectElement>(null);

  const isNew = scheduleId === null;
  const busy = create.isPending || update.isPending || remove.isPending;

  const set = <K extends keyof ScheduleFormState>(key: K, value: ScheduleFormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setSaved(false);
  };

  /** Map a server 422 problem onto the matching field (INV-8). */
  const applyProblem = (error: unknown) => {
    if (error instanceof ApiError && error.problem) {
      const code = error.problem.code;
      if (code === 'invalid_cron') {
        setErrors((e) => ({ ...e, cadence: error.displayMessage || 'Invalid cron expression.' }));
      } else if (code === 'invalid_timezone') {
        setErrors((e) => ({ ...e, timezone: error.displayMessage || 'Unknown timezone.' }));
      }
      setFormError(error.displayMessage);
    } else {
      setFormError('Something went wrong. Please try again.');
    }
  };

  const handleSave = () => {
    setFormError(null);
    const validation = validateForm(form);
    setErrors(validation);
    if (!isValid(validation)) {
      if (validation.assistantId) assistantRef.current?.focus();
      return;
    }

    if (isNew) {
      create.mutate(toCreateBody(form), {
        onSuccess: (s) => navigate(`/schedules/${s.id}`),
        onError: applyProblem,
      });
    } else {
      update.mutate(toUpdateBody(form), {
        onSuccess: () => setSaved(true),
        onError: applyProblem,
      });
    }
  };

  const handleDelete = () => {
    if (isNew || !scheduleId) return;
    remove.mutate(scheduleId, {
      onSuccess: () => navigate('/schedules'),
      onError: applyProblem,
    });
  };

  return (
    <form
      className="mx-auto max-w-2xl space-y-6 px-5 py-6"
      onSubmit={(e) => {
        e.preventDefault();
        handleSave();
      }}
      noValidate
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-base font-semibold">{isNew ? 'New schedule' : 'Edit schedule'}</h1>
          <p className="text-sm text-foreground-muted">
            {isNew
              ? 'Run one of your assistants automatically on a cadence.'
              : 'Adjust the cadence, timezone, inputs, or delivery.'}
          </p>
        </div>
        {current ? (
          <span className="rounded-full bg-surface-muted px-2.5 py-0.5 text-xs font-medium text-foreground-muted">
            {current.enabled ? 'Enabled' : 'Paused'}
          </span>
        ) : null}
      </header>

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

      {/* --- Assistant --- */}
      <section className="space-y-2">
        <label htmlFor="sched-form-assistant" className="block text-sm font-medium">
          Assistant<span className="text-danger"> *</span>
        </label>
        <AssistantSelect
          selectRef={assistantRef}
          value={form.assistantId}
          disabled={busy || !isNew}
          isPending={assistants.isPending}
          isError={assistants.isError}
          items={assistants.data?.items}
          onRetry={() => void assistants.refetch()}
          onChange={(id) => {
            set('assistantId', id);
            if (errors.assistantId) setErrors((e) => ({ ...e, assistantId: undefined }));
          }}
          error={errors.assistantId}
        />
        {!isNew ? (
          <p className="text-xs text-foreground-muted">
            The assistant is fixed for an existing schedule. Create a new schedule to run a
            different one.
          </p>
        ) : null}
      </section>

      {/* --- Cadence + timezone --- */}
      <section className="space-y-4 rounded-lg border border-border p-4">
        <CadenceInput
          value={form.cadence}
          disabled={busy}
          error={errors.cadence}
          onChange={(cadence) => {
            set('cadence', cadence);
            if (errors.cadence) setErrors((e) => ({ ...e, cadence: undefined }));
          }}
        />
        <TimezoneInput
          value={form.timezone}
          disabled={busy}
          error={errors.timezone}
          onChange={(tz) => {
            set('timezone', tz);
            if (errors.timezone) setErrors((e) => ({ ...e, timezone: undefined }));
          }}
        />
      </section>

      {/* --- Inputs --- */}
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Inputs</h2>
        <ParamsEditor rows={form.params} disabled={busy} onChange={(rows) => set('params', rows)} />
      </section>

      {/* --- Delivery --- */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium">Delivery</h2>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.deliverInbox}
            disabled={busy}
            onChange={(e) => set('deliverInbox', e.target.checked)}
            className="rounded border-border"
          />
          Land completed runs in the in-app run inbox
        </label>
        <label htmlFor="sched-digest" className="flex flex-col gap-1 text-xs text-foreground-muted">
          <span>Digest</span>
          <select
            id="sched-digest"
            value={form.digest}
            disabled={busy}
            onChange={(e) => set('digest', e.target.value as ScheduleFormState['digest'])}
            className="w-48 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
          >
            {DIGEST_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <p className="text-xs text-foreground-muted">
          External channels (email / Slack) aren’t configured here yet — runs land in-app.
        </p>
      </section>

      {/* --- Overlap --- */}
      <section className="space-y-2">
        <label htmlFor="sched-overlap" className="block text-sm font-medium">
          When a run is still active
        </label>
        <select
          id="sched-overlap"
          value={form.overlapPolicy}
          disabled={busy}
          onChange={(e) =>
            set('overlapPolicy', e.target.value as ScheduleFormState['overlapPolicy'])
          }
          className="w-full max-w-sm rounded-md border border-border bg-surface px-3 py-2 text-sm"
        >
          {OVERLAP_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label} — {o.hint}
            </option>
          ))}
        </select>
      </section>

      {/* --- Enabled --- */}
      <section>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.enabled}
            disabled={busy}
            onChange={(e) => set('enabled', e.target.checked)}
            className="rounded border-border"
          />
          {isNew ? 'Start firing immediately' : 'Enabled'}
        </label>
      </section>

      {/* --- Actions --- */}
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
        <button
          type="submit"
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name="check" className="shrink-0" />
          {isNew
            ? create.isPending
              ? 'Creating…'
              : 'Create schedule'
            : update.isPending
              ? 'Saving…'
              : 'Save'}
        </button>

        {!isNew ? (
          <button
            type="button"
            onClick={handleDelete}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-md border border-danger/50 px-3 py-1.5 text-sm font-medium text-danger hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-60"
          >
            <Icon name="trash" className="shrink-0" />
            {remove.isPending ? 'Deleting…' : 'Delete'}
          </button>
        ) : null}

        <button
          type="button"
          onClick={() => navigate('/schedules')}
          disabled={busy}
          className="ml-auto rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          Back to schedules
        </button>
      </div>
    </form>
  );
}

function AssistantSelect({
  selectRef,
  value,
  disabled,
  isPending,
  isError,
  items,
  onChange,
  onRetry,
  error,
}: {
  selectRef: React.Ref<HTMLSelectElement>;
  value: string;
  disabled: boolean;
  isPending: boolean;
  isError: boolean;
  items: Array<{ id: string; name: string }> | undefined;
  onChange: (id: string) => void;
  onRetry: () => void;
  error?: string;
}) {
  if (isPending) {
    return (
      <div
        role="status"
        aria-label="Loading assistants"
        className="lc-skeleton"
        style={{ width: '50%' }}
      />
    );
  }
  if (isError) {
    return (
      <div className="space-y-1 text-sm">
        <p className="text-danger">Couldn’t load your assistants.</p>
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-border px-2.5 py-1 text-sm hover:bg-surface-muted"
        >
          Retry
        </button>
      </div>
    );
  }
  if (!items || items.length === 0) {
    return (
      <p className="rounded-md border border-border bg-surface-muted p-3 text-xs text-foreground-muted">
        You have no assistants yet. Create one first, then schedule it.
      </p>
    );
  }
  return (
    <>
      <select
        id="sched-form-assistant"
        ref={selectRef}
        value={value}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? 'sched-form-assistant-error' : undefined}
        onChange={(e) => onChange(e.target.value)}
        className="w-full max-w-sm rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 aria-[invalid=true]:border-danger"
      >
        <option value="">Select an assistant…</option>
        {items.map((a) => (
          <option key={a.id} value={a.id}>
            {a.name}
          </option>
        ))}
      </select>
      {error ? (
        <p id="sched-form-assistant-error" role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </>
  );
}
