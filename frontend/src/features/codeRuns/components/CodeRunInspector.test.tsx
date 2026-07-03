/**
 * CodeRunInspector state coverage (#232). The inspector must render code +
 * stdout/stderr + exit/duration + artifact links for a succeeded run (AC-1);
 * show a live status + streamed output while running; surface a failed / timeout /
 * killed run's stderr and a denied run's refusal distinctly — never a blank pane
 * (AC-2); and keep long output inside its own scroll container (AC-3).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { CodeRunInspector } from './CodeRunInspector';
import type { CodeRunView } from '../model/view';

function view(over: Partial<CodeRunView> = {}): CodeRunView {
  return {
    runId: 'run-1',
    status: 'succeeded',
    code: 'print("hello world")',
    stdout: 'hello world\n',
    stderr: '',
    exitCode: 0,
    durationMs: 820,
    resourceUsage: { peak_memory_bytes: 2048, cpu_time_ms: 40, max_pids: 2, output_bytes: 12 },
    artifactIds: [],
    imageDigest: null,
    createdAt: '2026-07-02T00:00:00Z',
    startedAt: null,
    finishedAt: null,
    streaming: false,
    ...over,
  };
}

describe('CodeRunInspector', () => {
  it('renders code + stdout + exit + duration + resource usage for a succeeded run (AC-1)', () => {
    render(<CodeRunInspector view={view()} />);
    // Code is rendered through the markdown/highlight pipeline (a real code block).
    expect(screen.getByText(/print/)).toBeInTheDocument();
    // stdout output pane.
    const stdout = screen.getByLabelText('stdout output');
    expect(within(stdout).getByText(/hello world/)).toBeInTheDocument();
    // Exit + duration + status.
    expect(screen.getByText('Succeeded')).toBeInTheDocument();
    expect(screen.getByText('820ms')).toBeInTheDocument();
    expect(screen.getByText('2.0 KB')).toBeInTheDocument();
  });

  it('links produced artifacts when a resolver is given (AC-1)', () => {
    render(
      <CodeRunInspector
        view={view({ artifactIds: ['aaaaaaaa-1111-2222-3333-444444444444'] })}
        resolveArtifactHref={(id) => `/artifacts/${id}`}
      />,
    );
    const link = screen.getByRole('link', { name: /open output file/i });
    expect(link).toHaveAttribute('href', '/artifacts/aaaaaaaa-1111-2222-3333-444444444444');
  });

  it('shows artifacts as inert chips (still surfaced) when no resolver is given', () => {
    render(<CodeRunInspector view={view({ artifactIds: ['aaaaaaaa-1111'] })} />);
    expect(screen.getByLabelText('Produced artifacts')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /open output file/i })).not.toBeInTheDocument();
  });

  it('shows a live status + streamed output while running (AC-1)', () => {
    render(
      <CodeRunInspector
        view={view({ status: 'running', code: null, stdout: 'partial…', exitCode: null, durationMs: null, resourceUsage: null, streaming: true })}
      />,
    );
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.getByText(/Running code…/)).toBeInTheDocument();
    const stdout = screen.getByLabelText('stdout output');
    expect(within(stdout).getByText(/partial/)).toBeInTheDocument();
    // A live output pane is a polite log region so a screen reader hears new bytes.
    expect(stdout).toHaveAttribute('role', 'log');
  });

  it('surfaces a FAILED run’s stderr tail prominently — not a blank pane (AC-2)', () => {
    render(
      <CodeRunInspector
        view={view({ status: 'failed', exitCode: 1, stdout: '', stderr: 'Traceback…\nZeroDivisionError' })}
      />,
    );
    const alert = screen.getByRole('alert');
    expect(within(alert).getByText(/Code run failed/i)).toBeInTheDocument();
    expect(within(alert).getByText(/ZeroDivisionError/)).toBeInTheDocument();
  });

  it('surfaces a TIMEOUT run distinctly (AC-2)', () => {
    render(<CodeRunInspector view={view({ status: 'timeout', exitCode: null, stderr: 'killed by watchdog' })} />);
    const alert = screen.getByRole('alert');
    expect(within(alert).getByText(/timed out/i)).toBeInTheDocument();
  });

  it('surfaces a DENIED run as a policy refusal, not a stderr dump (AC-2)', () => {
    render(<CodeRunInspector view={view({ status: 'denied', exitCode: null, stdout: '', stderr: '' })} />);
    const alert = screen.getByRole('alert');
    expect(within(alert).getByText(/not allowed/i)).toBeInTheDocument();
    expect(screen.getByText('Denied')).toBeInTheDocument();
  });

  it('keeps long output inside its own scroll container (AC-3)', () => {
    const long = Array.from({ length: 500 }, (_, i) => `line ${i} ${'x'.repeat(200)}`).join('\n');
    render(<CodeRunInspector view={view({ stdout: long })} />);
    const stdout = screen.getByLabelText('stdout output');
    // The pane wraps + scrolls (overflow-auto + whitespace-pre-wrap) so the huge
    // output never forces whole-page scroll or breaks layout.
    expect(stdout.className).toMatch(/overflow-auto/);
    expect(stdout.className).toMatch(/whitespace-pre-wrap/);
    expect(stdout).toHaveTextContent(/line 499/);
  });

  it('renders a reproducibility line when an image digest is present (E3-7)', () => {
    render(<CodeRunInspector view={view({ imageDigest: 'sha256:deadbeef' })} />);
    expect(screen.getByText(/Reproducible/)).toBeInTheDocument();
    expect(screen.getByText('sha256:deadbeef')).toBeInTheDocument();
  });

  it('never renders a raw <script> from the code through the sanitizer', () => {
    // The code goes through rehype-sanitize; a script tag in the source is inert
    // text inside the code block, never executed markup.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { container } = render(
      <CodeRunInspector view={view({ code: 'x = "<script>alert(1)</script>"' })} />,
    );
    expect(container.querySelector('script')).toBeNull();
    spy.mockRestore();
  });
});
