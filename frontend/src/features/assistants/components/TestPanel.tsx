/**
 * TestPanel (#215, E6-5) — preview/test/debug an assistant before publishing, with
 * NO real side effect. Runs the assistant's working (draft) config against a sample
 * input (POST /assistants/{id}/test) and renders the debug trace: the effective
 * system prompt, the retrieval grounding, each tool call (args + result — a write
 * tool is *simulated*, `run_python` is *denied*), the streamed outputs, any typed
 * errors, and the wall-clock timing. Saved test cases (client-side, per assistant)
 * are re-runnable as lightweight regression checks (pass/fail on an expected
 * substring).
 *
 * Every async surface has its own state (idle / running / error+retry / result) — no
 * blank panes. The read-only guarantee is surfaced honestly: a banner states that no
 * real write occurs, and a simulated write renders a "simulated" chip so the builder
 * can see the write path was exercised without a side effect.
 *
 * A11y: the run button is a real <button>; the trace is a labelled region; the
 * disclosure reuses the shared RetrievalTrace; live regions announce run status.
 */
import { useMemo, useState } from 'react';
import { ApiError } from '@/api';
import type { AssistantTestToolCall, AssistantTestTrace } from '@/api';
import { Icon } from '@/ui';
import { RetrievalTrace, type TraceStep } from '@/ui';
import { StatusBadge } from '@/components/StatusBadge';
import { useTestAssistant } from '../model/queries';
import {
  loadTestCases,
  newCaseId,
  removeTestCase,
  upsertTestCase,
  verdictFor,
  type AssistantTestCase,
} from '../model/testCases';

interface TestPanelProps {
  /** The assistant head id (edit mode only — a draft with no id cannot be tested). */
  assistantId: string;
}

export function TestPanel({ assistantId }: TestPanelProps) {
  const [input, setInput] = useState('');
  const [cases, setCases] = useState<AssistantTestCase[]>(() => loadTestCases(assistantId));
  // The last-run case (if any) so we can show a pass/fail verdict against its expectation.
  const [ranCase, setRanCase] = useState<AssistantTestCase | null>(null);

  const test = useTestAssistant(assistantId);
  const trace = test.data ?? null;
  const busy = test.isPending;

  const run = (sample: string, testCase: AssistantTestCase | null) => {
    setRanCase(testCase);
    test.mutate({ input: sample.trim() || undefined });
  };

  const saveCurrentAsCase = () => {
    const name = window.prompt('Name this test case:', input.slice(0, 40) || 'Untitled case');
    if (name === null) return;
    const expected = window.prompt('Optional — expected text in the answer (for pass/fail):') ?? '';
    const next: AssistantTestCase = {
      id: newCaseId(),
      name: name.trim() || 'Untitled case',
      input,
      expected: expected.trim() || undefined,
      savedAt: Date.now(),
    };
    setCases(upsertTestCase(assistantId, next));
  };

  const deleteCase = (id: string) => setCases(removeTestCase(assistantId, id));

  const verdict = useMemo(
    () => (ranCase && trace ? verdictFor(ranCase, trace) : null),
    [ranCase, trace],
  );

  return (
    <section aria-labelledby="assistant-test-heading" className="space-y-4">
      <div>
        <h2 id="assistant-test-heading" className="text-sm font-medium">
          Test &amp; debug
        </h2>
        <p className="mt-1 flex items-start gap-1.5 text-xs text-foreground-muted">
          <Icon name="shield-check" className="mt-px shrink-0" aria-hidden="true" />
          <span>
            A safe preview of your working draft. Tools that write are <strong>simulated</strong>{' '}
            and code execution is <strong>disabled</strong> — no file, no external effect, nothing
            is saved.
          </span>
        </p>
      </div>

      {/* --- Run a sample input --- */}
      <div className="space-y-2">
        <label htmlFor="assistant-test-input" className="block text-xs font-medium">
          Sample input
        </label>
        <textarea
          id="assistant-test-input"
          value={input}
          disabled={busy}
          rows={3}
          onChange={(e) => setInput(e.target.value)}
          placeholder="e.g. Summarize our refund policy and cite the source."
          className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        />
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => run(input, null)}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            <Icon name="play" className="shrink-0" aria-hidden="true" />
            {busy ? 'Running…' : 'Run test'}
          </button>
          <button
            type="button"
            onClick={saveCurrentAsCase}
            disabled={busy || input.trim().length === 0}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            <Icon name="plus" className="shrink-0" aria-hidden="true" />
            Save as case
          </button>
        </div>
      </div>

      {/* --- Saved cases (client-side regression checks) --- */}
      {cases.length > 0 ? (
        <div className="space-y-1.5">
          <h3 className="text-xs font-medium text-foreground-muted">Saved cases</h3>
          <ul className="space-y-1" aria-label="Saved test cases">
            {cases.map((c) => (
              <li
                key={c.id}
                className="flex items-center gap-2 rounded-md border border-border bg-surface px-2.5 py-1.5 text-sm"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{c.name}</p>
                  <p className="truncate text-xs text-foreground-muted">{c.input}</p>
                </div>
                {c.expected ? (
                  <span
                    className="shrink-0 text-xs text-foreground-muted"
                    title="Expected in answer"
                  >
                    expects “{c.expected}”
                  </span>
                ) : null}
                <button
                  type="button"
                  onClick={() => {
                    setInput(c.input);
                    run(c.input, c);
                  }}
                  disabled={busy}
                  className="shrink-0 rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
                >
                  Re-run
                </button>
                <button
                  type="button"
                  onClick={() => deleteCase(c.id)}
                  disabled={busy}
                  aria-label={`Delete case ${c.name}`}
                  className="shrink-0 rounded-md border border-border p-1 text-foreground-muted hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
                >
                  <Icon name="trash" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* --- Run status / result --- */}
      <div aria-live="polite">
        {busy ? (
          <div role="status" className="flex items-center gap-2 text-sm text-foreground-muted">
            <span className="lc-skeleton" style={{ width: 16, height: 16, borderRadius: 8 }} />
            Running a read-only test…
          </div>
        ) : test.isError ? (
          <TestError error={test.error} onRetry={() => run(input, ranCase)} />
        ) : trace ? (
          <TraceView trace={trace} verdict={verdict} />
        ) : (
          <p className="text-xs text-foreground-muted">
            Run a sample input to see the prompt, retrieval, tool calls, and answer.
          </p>
        )}
      </div>
    </section>
  );
}

function TraceView({
  trace,
  verdict,
}: {
  trace: AssistantTestTrace;
  verdict: 'pass' | 'fail' | null;
}) {
  const retrievalSteps: TraceStep[] = trace.retrieval.map((r) => ({
    label: String((r as { documentName?: unknown }).documentName ?? 'source'),
  }));
  const summary = `${trace.retrieval.length} ${
    trace.retrieval.length === 1 ? 'passage' : 'passages'
  } · ${trace.durationMs} ms`;

  return (
    <div className="space-y-3" role="region" aria-label="Debug trace">
      {/* Verdict + status */}
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge tone={trace.succeeded ? 'ok' : 'degraded'}>
          {trace.succeeded ? 'Completed' : 'Did not complete'}
        </StatusBadge>
        <StatusBadge tone="pending" detail={`${trace.durationMs} ms`}>
          {trace.model}
        </StatusBadge>
        {verdict ? (
          <StatusBadge tone={verdict === 'pass' ? 'ok' : 'danger'}>
            {verdict === 'pass' ? 'Regression: pass' : 'Regression: fail'}
          </StatusBadge>
        ) : null}
      </div>

      {/* Errors (typed problem envelopes only) */}
      {trace.errors.length > 0 ? (
        <ul className="space-y-1" aria-label="Errors">
          {trace.errors.map((e, i) => (
            <li
              key={i}
              role="alert"
              className="rounded-md border border-danger/40 bg-danger/10 p-2 text-xs text-danger"
            >
              {String(
                (e as { detail?: unknown; title?: unknown }).detail ??
                  (e as { title?: unknown }).title ??
                  'The test run failed.',
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {/* Retrieval */}
      <RetrievalTrace summary={summary} steps={retrievalSteps} />

      {/* Tool activity */}
      {trace.toolCalls.length > 0 ? (
        <div className="space-y-1.5">
          <h3 className="text-xs font-medium text-foreground-muted">Tool calls</h3>
          <ul className="space-y-1.5" aria-label="Tool calls">
            {trace.toolCalls.map((call) => (
              <ToolCallRow key={call.callId} call={call} />
            ))}
          </ul>
        </div>
      ) : null}

      {/* Outputs */}
      <div className="space-y-1">
        <h3 className="text-xs font-medium text-foreground-muted">Answer</h3>
        <p className="whitespace-pre-wrap rounded-md border border-border bg-surface-muted p-2.5 text-sm">
          {trace.outputs || <span className="text-foreground-muted">No answer text.</span>}
        </p>
      </div>

      {/* Prompt (disclosure) */}
      <details className="rounded-md border border-border bg-surface">
        <summary className="cursor-pointer px-2.5 py-1.5 text-xs font-medium">
          System prompt
        </summary>
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap px-2.5 pb-2.5 text-xs text-foreground-muted">
          {trace.prompt}
        </pre>
      </details>
    </div>
  );
}

function ToolCallRow({ call }: { call: AssistantTestToolCall }) {
  const result = (call.result ?? {}) as { ok?: boolean; summary?: unknown; error?: unknown };
  const ok = result.ok === true;
  const summary = typeof result.summary === 'string' ? result.summary : undefined;
  const simulated = summary?.toLowerCase().includes('simulated') ?? false;
  const denied = typeof result.error === 'string' && result.error.includes('denied');

  return (
    <li className="rounded-md border border-border bg-surface px-2.5 py-1.5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{call.tool ?? 'tool'}</span>
        <StatusBadge tone={ok ? 'ok' : 'degraded'}>{ok ? 'ok' : 'blocked'}</StatusBadge>
        {simulated ? <StatusBadge tone="pending">simulated</StatusBadge> : null}
        {denied ? <StatusBadge tone="degraded">denied</StatusBadge> : null}
      </div>
      {call.args ? (
        <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap text-xs text-foreground-muted">
          {JSON.stringify(call.args, null, 2)}
        </pre>
      ) : null}
      {summary ? <p className="mt-0.5 text-xs text-foreground-muted">{summary}</p> : null}
    </li>
  );
}

function TestError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  const message =
    status === 404
      ? 'This assistant doesn’t exist, or you don’t have access to it.'
      : status === 401
        ? 'Your session expired. Sign in again to run a test.'
        : error instanceof ApiError
          ? error.displayMessage || 'The test run could not be started.'
          : 'The test run could not be started.';
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger"
    >
      <span className="flex items-center gap-1.5">
        <Icon name="alert-triangle" aria-hidden="true" />
        {message}
      </span>
      {status !== 401 && status !== 404 ? (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-danger/40 bg-surface px-2.5 py-1 text-xs text-danger hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
