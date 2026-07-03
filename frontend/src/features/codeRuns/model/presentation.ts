/**
 * Pure presentation mappers for the code-run inspector (#232, E15-7 / E6-5) —
 * status → tone/label, and human formatting for duration / bytes / exit code.
 * Kept out of JSX so components stay presentational and these map exactly once
 * (frontend/AGENTS.md). No I/O, no React — every edge (a denied run with no exit
 * code, a queued run with no timing) is unit-testable off the DOM.
 */
import type { StatusTone } from '@/ui';
import type { CodeRunStatus, ResourceUsage } from '@/api';

/** Human label for each point in the code-run lifecycle (ADR-0013 §4). */
export const CODE_RUN_STATUS_LABEL: Record<CodeRunStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  timeout: 'Timed out',
  killed: 'Killed',
  denied: 'Denied',
};

/**
 * The trust-signal dot tone per status. `succeeded` is ok; `running`/`queued`
 * are the muted/sync in-progress tones; `denied` is a warning (a policy refusal,
 * not a crash); `failed`/`timeout`/`killed` are danger (the run broke). Keeping
 * denied distinct from failed makes a policy block read as "not allowed", not
 * "your code errored" (§6).
 */
export const CODE_RUN_STATUS_TONE: Record<CodeRunStatus, StatusTone> = {
  queued: 'muted',
  running: 'sync',
  succeeded: 'ok',
  failed: 'danger',
  timeout: 'danger',
  killed: 'danger',
  denied: 'warn',
};

/** A one-line human sentence for a status, used as the collapsed inspector header. */
export const CODE_RUN_STATUS_HEADLINE: Record<CodeRunStatus, string> = {
  queued: 'Code run is queued…',
  running: 'Running code…',
  succeeded: 'Code ran successfully',
  failed: 'Code run failed',
  timeout: 'Code run timed out',
  killed: 'Code run was killed (out of memory or process limit)',
  denied: 'Code execution was not allowed',
};

/** Whether a code run is in a terminal state (drives polling + "live" affordance). */
export function isTerminalCodeRun(status: CodeRunStatus): boolean {
  return (
    status === 'succeeded' ||
    status === 'failed' ||
    status === 'timeout' ||
    status === 'killed' ||
    status === 'denied'
  );
}

/**
 * Whether a run is a hard failure (crashed / timed out / killed) — the states
 * whose stderr tail we surface prominently (AC-2). `denied` is handled distinctly
 * (a policy refusal, never a stderr dump).
 */
export function isFailureCodeRun(status: CodeRunStatus): boolean {
  return status === 'failed' || status === 'timeout' || status === 'killed';
}

/**
 * Format a wall-clock duration in ms as a short human string ("820ms", "1.4s",
 * "2m 05s"). Null/undefined → an em-dash placeholder (never a blank cell).
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const totalSeconds = ms / 1000;
  if (totalSeconds < 60) {
    // 1 decimal under a minute reads naturally (e.g. "1.4s"); trim a trailing .0.
    const s = totalSeconds.toFixed(1).replace(/\.0$/, '');
    return `${s}s`;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.round(totalSeconds % 60);
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
}

/**
 * Format a byte count as a human string ("512 B", "4.0 KB", "1.2 MB"). Null →
 * em-dash placeholder.
 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(1)} ${units[idx]}`;
}

/** One label/value row of the resource-usage grid. */
export interface UsageRow {
  label: string;
  value: string;
}

/**
 * The resource-usage grid rows for a finished run (ADR-0013 §4). Each field is
 * best-effort on the wire — a null field renders "—" rather than being hidden, so
 * the grid shape is stable and an absent measurement is honest (never fabricated).
 * Returns null when there is no usage object at all (queued/running).
 */
export function usageRows(usage: ResourceUsage | null | undefined): UsageRow[] | null {
  if (!usage) return null;
  return [
    { label: 'Peak memory', value: formatBytes(usage.peak_memory_bytes) },
    { label: 'CPU time', value: formatDuration(usage.cpu_time_ms) },
    { label: 'Processes', value: usage.max_pids != null ? String(usage.max_pids) : '—' },
    { label: 'Output size', value: formatBytes(usage.output_bytes) },
  ];
}

/** Human exit-code label; null (killed/timeout/denied/in-flight) → em-dash. */
export function formatExitCode(code: number | null | undefined): string {
  return code == null ? '—' : String(code);
}
