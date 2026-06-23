/**
 * DirectAnswerBlock (#84): the cited direct answer. The answer text renders
 * through the sanitizing markdown pipeline (NOT raw / no injected HTML); each
 * citation is a clickable chip that opens a SourceInspector showing the cited
 * passage; a citation whose result_id is absent from the page's results is
 * dropped (an answer never cites an un-retrievable passage — spec 0004 INV-3).
 */
import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { DirectAnswer, SearchResult } from '@/api';
import { DirectAnswerBlock } from './DirectAnswerBlock';

const result: SearchResult = {
  id: 'r1',
  title: 'PTO Policy 2026',
  snippet: 'Employees accrue 20 days of paid time off per year.',
  match_spans: [{ start: 0, end: 9 }],
  why_matched: 'semantic',
  source: 'upload',
  type: 'document',
  owner: 'Dana Ruiz',
  last_indexed: new Date().toISOString(),
  permission: 'allowed',
};

function byId(...rs: SearchResult[]) {
  return new Map(rs.map((r) => [r.id, r]));
}

describe('DirectAnswerBlock', () => {
  it('renders the answer text as sanitized markdown (bold rendered, no raw asterisks)', () => {
    const answer: DirectAnswer = {
      text: 'You accrue **20 days** of PTO.',
      citations: [{ result_id: 'r1', snippet: 'accrue 20 days' }],
    };
    render(<DirectAnswerBlock answer={answer} resultsById={byId(result)} />);
    const region = screen.getByRole('region', { name: /direct answer/i });
    // Bold is rendered as <strong>, not left as raw markdown.
    expect(within(region).getByText('20 days').tagName).toBe('STRONG');
    expect(within(region).queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it('opens the SourceInspector with the cited passage when a chip is clicked', async () => {
    const answer: DirectAnswer = {
      text: 'You accrue 20 days of PTO.',
      citations: [{ result_id: 'r1', snippet: 'accrue 20 days' }],
    };
    const user = userEvent.setup();
    render(<DirectAnswerBlock answer={answer} resultsById={byId(result)} />);

    // No inspector until a citation is opened.
    expect(screen.queryByRole('region', { name: /Source:/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /citation 1/i }));
    const inspector = screen.getByRole('region', { name: /Source: PTO Policy 2026/i });
    // The cited passage text is shown, highlighted.
    expect(within(inspector).getByText('accrue 20 days').tagName).toBe('MARK');
  });

  it('drops a citation whose result_id is not present in results (INV-3)', () => {
    const answer: DirectAnswer = {
      text: 'Answer.',
      citations: [
        { result_id: 'r1', snippet: 'a' },
        { result_id: 'ghost', snippet: 'b' }, // not in results → un-renderable
      ],
    };
    render(<DirectAnswerBlock answer={answer} resultsById={byId(result)} />);
    // Exactly one citation chip — the resolvable one.
    expect(screen.getAllByRole('button', { name: /citation/i })).toHaveLength(1);
  });
});
