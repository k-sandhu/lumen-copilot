/**
 * CodeRunInspector (#232, E15-7 / E6-5) — the presentational surface for one
 * sandbox code run: the exact code the agent ran (syntax-highlighted, read-only),
 * its streamed stdout/stderr, the exit status / duration / resource usage, and
 * links to the artifacts it produced. A denied / failed / timeout / killed run
 * shows its status clearly — never a blank pane (AC-2).
 *
 * Rendered, never raw (frontend/AGENTS.md): the code goes through the sanitizing
 * MarkdownView pipeline as a fenced ```python block (highlight + copy), never a
 * raw string and never dangerouslySetInnerHTML. stdout/stderr are plain captured
 * bytes (not markdown) rendered in independently-scrollable, wrapping <pre>
 * containers so long lines / large output scroll within their box and never break
 * the layout (AC-3).
 *
 * Pure/presentational: it takes a normalized CodeRunView (see model/view.ts) and
 * an optional artifact-link resolver; the container owns fetching + merging the
 * live stream with the read endpoint.
 */
import { MarkdownView } from '@/lib/markdown';
import { Icon, StatusDot } from '@/ui';
import type { CodeRunView } from '../model/view';
import {
  CODE_RUN_STATUS_HEADLINE,
  CODE_RUN_STATUS_LABEL,
  CODE_RUN_STATUS_TONE,
  formatDuration,
  formatExitCode,
  isFailureCodeRun,
  usageRows,
} from '../model/presentation';

export interface CodeRunInspectorProps {
  view: CodeRunView;
  /**
   * Resolve an artifact id to an href (the file panel / download). When omitted,
   * artifacts render as inert id chips (still surfaced, never hidden) so the
   * inspector is useful even before the artifact-file surface is wired in.
   */
  resolveArtifactHref?: (artifactId: string) => string | undefined;
  /** Optional heading id so a container can label the region. */
  headingId?: string;
  /** Explicit recovery for an unbounded queued/running execution. */
  onCancel?: () => void;
  cancelling?: boolean;
}

export function CodeRunInspector({
  view,
  resolveArtifactHref,
  headingId,
  onCancel,
  cancelling = false,
}: CodeRunInspectorProps) {
  const { status } = view;
  const tone = CODE_RUN_STATUS_TONE[status];
  const failure = isFailureCodeRun(status);
  const denied = status === 'denied';
  const stderrTail = view.stderr.trim();
  const usage = usageRows(view.resourceUsage);

  return (
    <section
      className="space-y-3"
      aria-labelledby={headingId}
      aria-busy={view.streaming || undefined}
    >
      {/* Status line: dot + label + timing meta. Always present — never blank. */}
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <StatusDot tone={tone} label={CODE_RUN_STATUS_LABEL[status]} />
        {view.streaming ? (
          <span
            className="inline-flex items-center gap-1 text-xs text-foreground-muted"
            aria-live="polite"
          >
            <StatusDot tone="sync" title="Running" />
            {CODE_RUN_STATUS_HEADLINE[status]}
          </span>
        ) : null}
        <dl className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-foreground-muted">
          <Meta label="Exit" value={formatExitCode(view.exitCode)} />
          <Meta label="Duration" value={formatDuration(view.durationMs)} />
        </dl>
        {(status === 'queued' || status === 'running') && onCancel ? (
          <button
            type="button"
            className="rounded-md border border-danger/50 px-2 py-1 text-xs text-danger hover:bg-danger/10 disabled:opacity-60"
            disabled={cancelling}
            onClick={onCancel}
          >
            {cancelling ? 'Cancelling…' : 'Cancel execution'}
          </button>
        ) : null}
      </header>

      {view.sandboxSessionId ? (
        <p className="text-[11px] text-foreground-muted">
          Reusable sandbox generation {view.sandboxGeneration ?? '—'} · root inside container ·
          offline
        </p>
      ) : null}

      {/* A denied run: a clear policy-refusal banner (AC-2), never a stderr dump. */}
      {denied ? (
        <div role="alert" className="space-y-1 rounded-lg border border-warn/50 bg-warn/10 p-3">
          <p className="flex items-center gap-1.5 text-sm font-semibold text-warn">
            <Icon name="shield-x" className="shrink-0" />
            Code execution was not allowed
          </p>
          <p className="text-sm text-foreground-muted">
            This run was refused before it executed — code execution may be disabled for your
            workspace, or blocked by its package policy.
          </p>
        </div>
      ) : null}

      {/* A hard failure: surface the stderr tail prominently (AC-2). */}
      {failure ? (
        <div role="alert" className="space-y-1 rounded-lg border border-danger/50 bg-danger/10 p-3">
          <p className="flex items-center gap-1.5 text-sm font-semibold text-danger">
            <Icon name="alert-triangle" className="shrink-0" />
            {CODE_RUN_STATUS_HEADLINE[status]}
          </p>
          {stderrTail ? (
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-muted p-2 font-mono text-xs text-foreground">
              {stderrTail}
            </pre>
          ) : (
            <p className="text-sm text-foreground-muted">
              No error output was captured for this run.
            </p>
          )}
        </div>
      ) : null}

      {/* The code the agent ran — rendered, never raw. Null until the record loads. */}
      <div className="space-y-1">
        <h3 id={headingId} className="text-xs font-medium text-foreground-muted">
          Code
        </h3>
        {view.code != null ? (
          <div className="max-h-72 overflow-auto rounded-md border border-border">
            <MarkdownView className="text-xs">{toPythonBlock(view.code)}</MarkdownView>
          </div>
        ) : (
          <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-foreground-muted">
            {view.streaming
              ? 'Loading the code the agent ran…'
              : 'The code for this run isn’t available.'}
          </p>
        )}
      </div>

      {view.requestedPackages.length > 0 ? (
        <div className="space-y-1">
          <h3 className="text-xs font-medium text-foreground-muted">
            Packages installed for this run
          </h3>
          <ul className="flex flex-wrap gap-1.5" aria-label="Requested Python packages">
            {view.requestedPackages.map((requirement) => (
              <li
                key={requirement}
                className="rounded-md border border-border bg-surface-muted px-2 py-1 font-mono text-xs"
              >
                {requirement}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Output — stdout + stderr, each in its own scrollable, wrapping box (AC-3). */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <OutputPane
          label="stdout"
          text={view.stdout}
          streaming={view.streaming}
          emptyHint={view.streaming ? 'Waiting for output…' : 'No standard output.'}
        />
        <OutputPane
          label="stderr"
          text={view.stderr}
          tone="muted"
          streaming={view.streaming}
          emptyHint={view.streaming ? 'Waiting for output…' : 'No standard error.'}
        />
      </div>

      {/* Resource usage (present once finished). */}
      {usage ? (
        <div className="space-y-1">
          <h3 className="text-xs font-medium text-foreground-muted">Resource usage</h3>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
            {usage.map((row) => (
              <div key={row.label} className="flex flex-col">
                <dt className="text-foreground-muted">{row.label}</dt>
                <dd className="font-mono">{row.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}

      {/* Produced artifacts — links to the files the run emitted. */}
      {view.artifactIds.length > 0 ? (
        <div className="space-y-1">
          <h3 className="text-xs font-medium text-foreground-muted">
            Output files ({view.artifactIds.length})
          </h3>
          <ul className="flex flex-wrap gap-1.5" aria-label="Produced artifacts">
            {view.artifactIds.map((id) => {
              const href = resolveArtifactHref?.(id);
              return (
                <li key={id}>
                  {href ? (
                    <a
                      href={href}
                      className="inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2 py-1 font-mono text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      aria-label={`Open output file ${id}`}
                    >
                      <Icon name="file-text" className="shrink-0" />
                      {shortId(id)}
                    </a>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface-muted px-2 py-1 font-mono text-xs text-foreground-muted">
                      <Icon name="file-text" className="shrink-0" />
                      {shortId(id)}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {/* Reproducibility (E3-7): the pinned image digest, when present. */}
      {view.imageDigest ? (
        <p className="flex items-center gap-1 text-[11px] text-foreground-muted">
          <Icon name="lock" className="shrink-0" />
          <span className="font-medium">Reproducible</span> — pinned image
          <span className="ml-1 truncate font-mono" title={view.imageDigest}>
            {view.imageDigest}
          </span>
        </p>
      ) : null}
    </section>
  );
}

/** stdout/stderr pane — scrollable + wrapping so long output never breaks layout. */
function OutputPane({
  label,
  text,
  emptyHint,
  streaming,
  tone,
}: {
  label: string;
  text: string;
  emptyHint: string;
  streaming: boolean;
  tone?: 'muted';
}) {
  const hasText = text.length > 0;
  return (
    <div className="min-w-0 space-y-1">
      <h3 className="font-mono text-[11px] uppercase tracking-wide text-foreground-muted">
        {label}
      </h3>
      <pre
        className={`max-h-52 min-h-[3rem] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface-muted p-2 font-mono text-xs ${
          tone === 'muted' ? 'text-foreground-muted' : 'text-foreground'
        }`}
        {...(streaming
          ? { role: 'log', 'aria-live': 'polite' as const, 'aria-atomic': false }
          : {})}
        aria-label={`${label} output`}
        tabIndex={0}
      >
        {hasText ? (
          <>
            {text}
            {streaming ? <span className="lc-caret" aria-hidden="true" /> : null}
          </>
        ) : (
          <span className="text-foreground-muted">{emptyHint}</span>
        )}
      </pre>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1">
      <dt className="font-medium">{label}:</dt>
      <dd className="font-mono">{value}</dd>
    </div>
  );
}

/**
 * Wrap the exact source in a fenced ```python block so the shared MarkdownView
 * highlights it. A stray ``` inside the source can't break out of the fence via
 * markdown injection — rehype-sanitize strips anything dangerous — but we lengthen
 * the fence past the longest backtick run in the code so a literal ``` renders as
 * text rather than closing the block early.
 */
function toPythonBlock(code: string): string {
  const longestRun = (code.match(/`+/g) ?? []).reduce((m, run) => Math.max(m, run.length), 0);
  const fence = '`'.repeat(Math.max(3, longestRun + 1));
  return `${fence}python\n${code}\n${fence}`;
}

/** Short display form of a uuid artifact id (first segment) for a compact chip. */
function shortId(id: string): string {
  const head = id.split('-')[0] ?? id;
  return head.length >= 8 ? head : id.slice(0, 8);
}
