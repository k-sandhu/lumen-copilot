/**
 * TestPanel (#215, E6-5) — the preview/test/debug panel:
 *   • RUN a sample input → POST /assistants/{id}/test → render the debug trace
 *     (prompt, retrieval, tool calls with args/result, outputs, timing);
 *   • the READ-ONLY guarantee is surfaced: a simulated write renders a "simulated"
 *     chip and a denied run_python a "denied" chip (no real side effect);
 *   • SAVE a case (client-side) and RE-RUN it, with a pass/fail regression verdict;
 *   • ERROR + retry on a transient failure; 404 (non-owned) is honest, no retry.
 *
 * Rendered against a mocked fetch routed by URL so a contract match is an
 * integration match (ADR-0006 Phase 1).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { clearAccessToken, setAccessToken } from '@/api';
import type { AssistantTestTrace } from '@/api';
import { TestPanel } from './TestPanel';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

function makeTrace(overrides: Partial<AssistantTestTrace> = {}): AssistantTestTrace {
  return {
    prompt: 'You are a tax report builder.\n\nGrounding rules…',
    input: 'what is the deduction?',
    model: 'openrouter/openai/gpt-5.5',
    retrieval: [{ documentName: 'taxes.pdf', snippet: '…$14,600…' }],
    toolCalls: [
      {
        callId: 'c1',
        tool: 'search_text',
        args: { query: 'x' },
        result: { ok: true, summary: 'searched' },
      },
    ],
    outputs: 'The 2024 standard deduction is $14,600.',
    errors: [],
    succeeded: true,
    durationMs: 42,
    ...overrides,
  };
}

function mockTest(response: () => Response) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    if (url.includes('/assistants/a1/test') && method === 'POST') {
      return Promise.resolve(response());
    }
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

beforeEach(() => {
  setAccessToken('jwt');
  window.localStorage.clear();
});
afterEach(() => {
  clearAccessToken();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe('TestPanel — run + debug trace', () => {
  it('starts idle with the read-only guarantee stated', () => {
    mockTest(() => json(makeTrace()));
    renderWithQuery(<TestPanel assistantId="a1" />);
    // The safe-preview banner names the guarantee.
    expect(screen.getByText(/simulated/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is saved/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Run a sample input to see the prompt, retrieval, tool calls/i),
    ).toBeInTheDocument();
  });

  it('runs a sample input and renders the debug trace (AC-3)', async () => {
    mockTest(() => json(makeTrace()));
    const user = userEvent.setup();
    renderWithQuery(<TestPanel assistantId="a1" />);

    await user.type(screen.getByLabelText(/sample input/i), 'what is the deduction?');
    await user.click(screen.getByRole('button', { name: /run test/i }));

    // The trace region shows outputs, tool call + args, timing, and the prompt.
    const region = await screen.findByRole('region', { name: /debug trace/i });
    expect(within(region).getByText(/The 2024 standard deduction is \$14,600/)).toBeInTheDocument();
    expect(within(region).getByText('search_text')).toBeInTheDocument();
    expect(within(region).getByText(/"query": "x"/)).toBeInTheDocument();
    expect(within(region).getByText(/42 ms/)).toBeInTheDocument();
    // The system prompt is disclosed.
    expect(within(region).getByText(/tax report builder/i)).toBeInTheDocument();
  });

  it('surfaces a SIMULATED write and a DENIED run_python (no real side effect)', async () => {
    mockTest(() =>
      json(
        makeTrace({
          toolCalls: [
            {
              callId: 'w1',
              tool: 'write_file',
              args: { filename: 'r.md' },
              result: { ok: true, summary: 'simulated write: r.md' },
            },
            {
              callId: 'p1',
              tool: 'run_python',
              args: { code: 'print(1)' },
              result: { ok: false, error: 'code_execution_denied', summary: 'denied' },
            },
          ],
        }),
      ),
    );
    const user = userEvent.setup();
    renderWithQuery(<TestPanel assistantId="a1" />);
    await user.type(screen.getByLabelText(/sample input/i), 'build a report');
    await user.click(screen.getByRole('button', { name: /run test/i }));

    const region = await screen.findByRole('region', { name: /debug trace/i });
    expect(within(region).getByText('write_file')).toBeInTheDocument();
    // The write path was exercised but simulated (chip + summary both say so).
    expect(within(region).getAllByText(/simulated/i).length).toBeGreaterThan(0);
    expect(within(region).getByText('run_python')).toBeInTheDocument();
    expect(within(region).getAllByText(/denied/i).length).toBeGreaterThan(0);
  });

  it('shows an ERROR + retry on a transient failure', async () => {
    mockTest(() => problem(500, 'Server Error'));
    const user = userEvent.setup();
    renderWithQuery(<TestPanel assistantId="a1" />);
    await user.type(screen.getByLabelText(/sample input/i), 'x');
    await user.click(screen.getByRole('button', { name: /run test/i }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('shows an honest 404 with NO retry (non-owned / cross-tenant)', async () => {
    mockTest(() => problem(404, 'Not Found'));
    const user = userEvent.setup();
    renderWithQuery(<TestPanel assistantId="a1" />);
    await user.type(screen.getByLabelText(/sample input/i), 'x');
    await user.click(screen.getByRole('button', { name: /run test/i }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/don’t have access/i)).toBeInTheDocument();
    expect(within(alert).queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });
});

describe('TestPanel — saved cases (client-side regression)', () => {
  it('saves a case and re-runs it, showing a pass verdict', async () => {
    mockTest(() => json(makeTrace({ outputs: 'The answer contains 14,600.' })));
    // Name + expected substring prompts.
    const prompt = vi.spyOn(window, 'prompt');
    prompt.mockReturnValueOnce('Deduction check'); // name
    prompt.mockReturnValueOnce('14,600'); // expected
    const user = userEvent.setup();
    renderWithQuery(<TestPanel assistantId="a1" />);

    await user.type(screen.getByLabelText(/sample input/i), 'what is the deduction?');
    await user.click(screen.getByRole('button', { name: /save as case/i }));

    // The saved case appears with its expectation.
    const cases = await screen.findByRole('list', { name: /saved test cases/i });
    expect(within(cases).getByText('Deduction check')).toBeInTheDocument();

    // Re-run it → a PASS verdict (the expected substring is in the outputs).
    await user.click(within(cases).getByRole('button', { name: /re-run/i }));
    await waitFor(() => expect(screen.getByText(/regression: pass/i)).toBeInTheDocument());
  });

  it('shows a FAIL verdict when the expected substring is absent', async () => {
    mockTest(() => json(makeTrace({ outputs: 'Something else entirely.' })));
    const prompt = vi.spyOn(window, 'prompt');
    prompt.mockReturnValueOnce('Miss'); // name
    prompt.mockReturnValueOnce('14,600'); // expected
    const user = userEvent.setup();
    renderWithQuery(<TestPanel assistantId="a1" />);

    await user.type(screen.getByLabelText(/sample input/i), 'q');
    await user.click(screen.getByRole('button', { name: /save as case/i }));
    const cases = await screen.findByRole('list', { name: /saved test cases/i });
    await user.click(within(cases).getByRole('button', { name: /re-run/i }));
    await waitFor(() => expect(screen.getByText(/regression: fail/i)).toBeInTheDocument());
  });

  it('deletes a saved case', async () => {
    mockTest(() => json(makeTrace()));
    const prompt = vi.spyOn(window, 'prompt');
    prompt.mockReturnValueOnce('Removable'); // name
    prompt.mockReturnValueOnce(''); // no expectation
    const user = userEvent.setup();
    renderWithQuery(<TestPanel assistantId="a1" />);
    await user.type(screen.getByLabelText(/sample input/i), 'q');
    await user.click(screen.getByRole('button', { name: /save as case/i }));

    const cases = await screen.findByRole('list', { name: /saved test cases/i });
    expect(within(cases).getByText('Removable')).toBeInTheDocument();
    await user.click(within(cases).getByRole('button', { name: /delete case removable/i }));
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: /saved test cases/i })).not.toBeInTheDocument(),
    );
  });
});
