/**
 * RunDetail (#237, AC-2/AC-3) — the full record of one run: status, trigger,
 * inputs, the persisted transcript (tool calls + deltas), the grounded citations
 * (click a chip → the kit SourceInspector), output artifacts, and — for a
 * failed/escalated run — the typed error/escalation reason prominently, never a
 * blank pane (AC-3).
 *
 * Reuses the design kit: RetrievalTrace (the transcript disclosure),
 * CitationChip + SourceInspector (the citations), StatusDot (status). The
 * assistant answer is rendered through the sanitizing MarkdownView pipeline
 * (frontend/AGENTS.md "rendered, never raw"). A non-terminal run polls (queries.ts
 * refetchInterval) so an in-progress run's status/transcript catch up.
 *
 * Two independently-scrollable panes: the run body (left) and the source inspector
 * (right), each in a min-h-0 flex cell so long transcripts never force whole-page
 * scroll.
 */
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Citation, Run } from '@/api';
import { MarkdownView } from '@/lib/markdown';
import { CitationChip, Icon, RetrievalTrace, SourceInspector, StatusDot } from '@/ui';
import { useRun } from '../model/queries';
import {
  RUN_STATUS_LABEL,
  RUN_STATUS_TONE,
  TRIGGER_LABEL,
  formatDateTime,
  isFailureOrEscalation,
  isTerminal,
} from '../model/presentation';
import {
  assembleAnswer,
  citationToPassage,
  toTranscript,
  traceSummary,
} from '../model/transcript';
import { EscalationActions } from './EscalationActions';
import { ErrorState, SkeletonRows } from './StateViews';

export function RunDetail({ runId }: { runId: string }) {
  const query = useRun(runId);

  if (query.isPending) {
    return (
      <div className="mx-auto max-w-3xl px-5 py-6">
        <SkeletonRows count={4} label="Loading run…" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="mx-auto max-w-3xl px-5 py-6">
        <ErrorState
          title="Couldn’t load run"
          error={query.error}
          onRetry={() => void query.refetch()}
          busy={query.isFetching}
        />
      </div>
    );
  }

  return <RunBody run={query.data} live={query.isFetching && !isTerminal(query.data.status)} />;
}

function RunBody({ run, live }: { run: Run; live: boolean }) {
  const [selected, setSelected] = useState<Citation | null>(null);

  const answer = useMemo(() => assembleAnswer(run.steps), [run.steps]);
  const transcript = useMemo(() => toTranscript(run.steps), [run.steps]);
  const citations = run.citations ?? [];
  const flagged = isFailureOrEscalation(run.status);

  const outputsText = useMemo(() => {
    if (!run.outputs || Object.keys(run.outputs).length === 0) return null;
    try {
      return JSON.stringify(run.outputs, null, 2);
    } catch {
      return null;
    }
  }, [run.outputs]);

  const inputsText = useMemo(() => {
    if (!run.inputs || Object.keys(run.inputs).length === 0) return null;
    try {
      return JSON.stringify(run.inputs, null, 2);
    } catch {
      return null;
    }
  }, [run.inputs]);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
      {/* --- Run body (independently scrollable) --- */}
      <div className="min-h-0 overflow-y-auto px-5 py-6">
        <div className="mx-auto max-w-2xl space-y-6">
          <header className="space-y-2">
            <Link
              to="/runs"
              className="inline-flex items-center gap-1 text-sm text-foreground-muted hover:text-foreground"
            >
              <Icon name="corner-down-left" className="shrink-0" />
              Back to run history
            </Link>
            <div className="flex flex-wrap items-center gap-2">
              <StatusDot tone={RUN_STATUS_TONE[run.status]} label={RUN_STATUS_LABEL[run.status]} />
              <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-foreground-muted">
                {TRIGGER_LABEL[run.trigger]}
              </span>
              {live ? (
                <span className="inline-flex items-center gap-1 text-xs text-foreground-muted" aria-live="polite">
                  <StatusDot tone="sync" title="Live" />
                  Updating…
                </span>
              ) : null}
            </div>
            <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-foreground-muted">
              <Meta label="Started" value={formatDateTime(run.started_at)} />
              <Meta label="Finished" value={formatDateTime(run.finished_at)} />
            </dl>
          </header>

          {/* Failure / escalation surfaced first (AC-3) — never a blank pane. */}
          {flagged ? (
            <section
              role="alert"
              className={
                run.status === 'failed'
                  ? 'space-y-1 rounded-lg border border-danger/50 bg-danger/10 p-4'
                  : 'space-y-1 rounded-lg border border-warn/50 bg-warn/10 p-4'
              }
            >
              <p className={`flex items-center gap-1.5 text-sm font-semibold ${run.status === 'failed' ? 'text-danger' : 'text-warn'}`}>
                <Icon name="alert-triangle" className="shrink-0" />
                {run.status === 'failed' ? 'This run failed' : 'This run was escalated — it needs a human'}
              </p>
              {run.error ? (
                <>
                  <p className="text-sm">{run.error.message}</p>
                  <p className="font-mono text-xs text-foreground-muted">code: {run.error.code}</p>
                </>
              ) : (
                <p className="text-sm text-foreground-muted">No reason was recorded for this run.</p>
              )}
              {/* The human handoff (E7-5, #239) — resume / cancel / reroute the escalated run. */}
              {run.status === 'escalated' ? <EscalationActions run={run} /> : null}
            </section>
          ) : null}

          {run.summary ? (
            <section className="space-y-1">
              <h2 className="text-sm font-medium">Summary</h2>
              <p className="text-sm text-foreground-muted">{run.summary}</p>
            </section>
          ) : null}

          {/* Transcript trace (tool calls / citations) via the kit RetrievalTrace. */}
          <section className="space-y-2">
            <h2 className="text-sm font-medium">Transcript</h2>
            <RetrievalTrace
              summary={traceSummary(run.steps, run.citations)}
              defaultOpen={transcript.length > 0}
              steps={transcript.map((t) => ({
                label: t.detail ? `${t.label} — ${firstLine(t.detail)}` : t.label,
                excluded: t.kind === 'error',
              }))}
            />
          </section>

          {/* The assistant's answer — rendered, never raw. */}
          <section className="space-y-2">
            <h2 className="text-sm font-medium">Output</h2>
            {answer ? (
              <MarkdownView className="text-sm">{answer}</MarkdownView>
            ) : run.status === 'queued' || run.status === 'running' ? (
              <p className="text-sm text-foreground-muted">The run is still producing output…</p>
            ) : (
              <p className="text-sm text-foreground-muted">This run produced no text output.</p>
            )}

            {citations.length > 0 ? (
              <div className="flex flex-wrap items-center gap-1.5 pt-1">
                <span className="text-xs text-foreground-muted">Citations:</span>
                {citations.map((c, i) => (
                  <CitationChip
                    key={c.id}
                    index={i + 1}
                    sourceTitle={c.document_name}
                    active={selected?.id === c.id}
                    onClick={() => setSelected(c)}
                  />
                ))}
              </div>
            ) : null}
          </section>

          {/* Output artifacts (structured outputs). */}
          {outputsText ? (
            <section className="space-y-2">
              <h2 className="text-sm font-medium">Output artifacts</h2>
              <pre className="lc-pre overflow-x-auto rounded-md border border-border bg-surface-muted p-3 text-xs">
                {outputsText}
              </pre>
            </section>
          ) : null}

          {/* Inputs snapshot. */}
          {inputsText ? (
            <section className="space-y-2">
              <h2 className="text-sm font-medium">Inputs</h2>
              <pre className="lc-pre overflow-x-auto rounded-md border border-border bg-surface-muted p-3 text-xs">
                {inputsText}
              </pre>
            </section>
          ) : null}
        </div>
      </div>

      {/* --- Source inspector pane (independently scrollable) --- */}
      <aside className="hidden min-h-0 overflow-y-auto border-l border-border bg-surface lg:block">
        <div className="p-4">
          {selected ? (
            <SourceInspector
              title={selected.document_name}
              passage={citationToPassage(selected)}
              onClose={() => setSelected(null)}
            />
          ) : (
            <div className="lc-state">
              <Icon name="file-text" />
              <span>Select a citation to view its source.</span>
            </div>
          )}
        </div>
      </aside>

      {/* Mobile citation inspector (no side pane) — a dismissible panel. */}
      {selected ? (
        <div className="border-t border-border bg-surface p-4 lg:hidden">
          <SourceInspector
            title={selected.document_name}
            passage={citationToPassage(selected)}
            onClose={() => setSelected(null)}
          />
        </div>
      ) : null}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1">
      <dt className="font-medium">{label}:</dt>
      <dd>{value}</dd>
    </div>
  );
}

function firstLine(text: string): string {
  const line = text.split('\n')[0] ?? '';
  return line.length > 80 ? `${line.slice(0, 80)}…` : line;
}
