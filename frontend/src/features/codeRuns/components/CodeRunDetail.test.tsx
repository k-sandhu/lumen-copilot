/**
 * CodeRunDetail state coverage (#232) — the standalone after-the-fact inspection
 * view driven purely by GET /code-runs/{id}. Covers loading, error+retry, the
 * honest 404 dead-end, and a populated succeeded / denied record.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { CodeRun } from '@/api';

const getCodeRun = vi.fn<() => Promise<CodeRun>>();

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getCodeRun: () => getCodeRun() };
});

import { CodeRunDetail } from './CodeRunDetail';

const SUCCEEDED: CodeRun = {
  id: 'run-1',
  status: 'succeeded',
  code: 'print("hi")',
  stdout: 'hi\n',
  stderr: '',
  exit_code: 0,
  duration_ms: 120,
  resource_usage: { peak_memory_bytes: 2048 },
  artifact_ids: [],
  created_at: '2026-07-02T00:00:00Z',
};

const DENIED: CodeRun = {
  id: 'run-2',
  status: 'denied',
  code: 'import os; os.system("rm -rf /")',
  stdout: '',
  stderr: '',
  artifact_ids: [],
  created_at: '2026-07-02T00:00:00Z',
};

beforeEach(() => getCodeRun.mockReset());

describe('CodeRunDetail', () => {
  it('renders a loading skeleton', () => {
    getCodeRun.mockReturnValue(new Promise<CodeRun>(() => {}));
    renderWithQuery(<CodeRunDetail runId="run-1" />);
    expect(screen.getByText(/loading code run/i)).toBeInTheDocument();
  });

  it('renders an actionable retry on error', async () => {
    getCodeRun.mockRejectedValue(new ApiError('boom', 500));
    renderWithQuery(<CodeRunDetail runId="run-1" />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages a 404 as existence non-disclosure without a retry (INV-1/INV-2)', async () => {
    getCodeRun.mockRejectedValue(new ApiError('nope', 404));
    renderWithQuery(<CodeRunDetail runId="run-1" />);
    expect(await screen.findByText(/no longer exists/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders the code + output + status for a succeeded run', async () => {
    getCodeRun.mockResolvedValue(SUCCEEDED);
    renderWithQuery(<CodeRunDetail runId="run-1" />);
    expect(await screen.findByText(/print/)).toBeInTheDocument();
    expect(screen.getByText('Succeeded')).toBeInTheDocument();
    const stdout = screen.getByLabelText('stdout output');
    expect(within(stdout).getByText(/hi/)).toBeInTheDocument();
  });

  it('surfaces a denied run as a refusal — never a blank pane (AC-2)', async () => {
    getCodeRun.mockResolvedValue(DENIED);
    renderWithQuery(<CodeRunDetail runId="run-2" />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/not allowed/i)).toBeInTheDocument();
  });
});
