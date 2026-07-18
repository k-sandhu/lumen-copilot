/**
 * Pure presentation mappers for the code-run inspector (#232). Covers the human
 * formatters (duration / bytes / exit code), the status classifiers, and the
 * resource-usage grid — including the honest "—" placeholders for absent fields.
 */
import { describe, it, expect } from 'vitest';
import {
  CODE_RUN_STATUS_LABEL,
  CODE_RUN_STATUS_TONE,
  formatBytes,
  formatDuration,
  formatExitCode,
  isFailureCodeRun,
  isTerminalCodeRun,
  usageRows,
} from './presentation';

describe('formatDuration', () => {
  it('sub-second → ms', () => {
    expect(formatDuration(0)).toBe('0ms');
    expect(formatDuration(820)).toBe('820ms');
  });
  it('seconds with one decimal, trimming a trailing .0', () => {
    expect(formatDuration(1400)).toBe('1.4s');
    expect(formatDuration(2000)).toBe('2s');
  });
  it('minutes + zero-padded seconds', () => {
    expect(formatDuration(125_000)).toBe('2m 05s');
  });
  it('null / negative → em-dash', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(undefined)).toBe('—');
    expect(formatDuration(-5)).toBe('—');
  });
});

describe('formatBytes', () => {
  it('bytes / KB / MB', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(4096)).toBe('4.0 KB');
    expect(formatBytes(1_572_864)).toBe('1.5 MB');
  });
  it('null → em-dash', () => {
    expect(formatBytes(null)).toBe('—');
  });
});

describe('formatExitCode', () => {
  it('renders 0 and non-zero', () => {
    expect(formatExitCode(0)).toBe('0');
    expect(formatExitCode(1)).toBe('1');
  });
  it('null (killed/timeout/denied) → em-dash', () => {
    expect(formatExitCode(null)).toBe('—');
  });
});

describe('status classifiers', () => {
  it('terminal covers all non-queued/running', () => {
    expect(isTerminalCodeRun('queued')).toBe(false);
    expect(isTerminalCodeRun('running')).toBe(false);
    for (const s of ['succeeded', 'failed', 'timeout', 'killed', 'denied'] as const) {
      expect(isTerminalCodeRun(s)).toBe(true);
    }
  });
  it('failure is crashed/timeout/killed — NOT denied', () => {
    expect(isFailureCodeRun('failed')).toBe(true);
    expect(isFailureCodeRun('timeout')).toBe(true);
    expect(isFailureCodeRun('killed')).toBe(true);
    expect(isFailureCodeRun('denied')).toBe(false);
    expect(isFailureCodeRun('succeeded')).toBe(false);
  });
  it('denied is a warning tone, not danger (a refusal, not a crash)', () => {
    expect(CODE_RUN_STATUS_TONE.denied).toBe('warn');
    expect(CODE_RUN_STATUS_TONE.failed).toBe('danger');
    expect(CODE_RUN_STATUS_TONE.succeeded).toBe('ok');
    expect(CODE_RUN_STATUS_LABEL.timeout).toBe('Timed out');
  });
});

describe('usageRows', () => {
  it('null usage → null (queued/running)', () => {
    expect(usageRows(null)).toBeNull();
    expect(usageRows(undefined)).toBeNull();
  });
  it('renders every field, with "—" for absent best-effort measurements', () => {
    const rows = usageRows({
      peak_memory_bytes: 2048,
      cpu_time_ms: null,
      max_pids: 3,
      output_bytes: null,
    });
    expect(rows).not.toBeNull();
    const byLabel = Object.fromEntries((rows ?? []).map((r) => [r.label, r.value]));
    expect(byLabel['Peak memory']).toBe('2.0 KB');
    expect(byLabel['CPU time']).toBe('—');
    expect(byLabel['Processes']).toBe('3');
    expect(byLabel['Output size']).toBe('—');
  });
});
