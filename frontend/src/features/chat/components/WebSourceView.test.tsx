/**
 * WebSourceView (#221 AC-2): the inspector pane for a web citation. Renders the
 * cited snippet distinctly (globe + host), links out SAFELY
 * (rel="noopener noreferrer", target=_blank), and covers the empty-snippet state
 * (never a blank pane). It must NOT try to fetch corpus document bytes.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WebSourceView } from './WebSourceView';
import type { UiCitation } from '../model/citation';

const WEB: UiCitation = {
  id: 'w1',
  documentId: '',
  documentName: 'EU AI Act overview',
  chunkId: '',
  snippet: 'The Act phases in through 2026.',
  charStart: 0,
  charEnd: 30,
  url: 'https://www.example.org/eu-ai-act',
  webTitle: 'EU AI Act overview',
};

describe('WebSourceView', () => {
  it('renders the host + snippet and a safe external link (AC-2)', () => {
    render(<WebSourceView citation={WEB} onClose={() => {}} />);
    // www. stripped host is shown (appears in header + inspector meta).
    expect(screen.getAllByText('example.org').length).toBeGreaterThan(0);
    expect(screen.getByText(/phases in through 2026/i)).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /open page/i });
    expect(link).toHaveAttribute('href', WEB.url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('covers the empty-snippet state without a blank pane', () => {
    render(<WebSourceView citation={{ ...WEB, snippet: '' }} onClose={() => {}} />);
    expect(screen.getByText(/no excerpt was captured/i)).toBeInTheDocument();
    // Still links out so the user can read the source.
    expect(screen.getByRole('link', { name: /open page/i })).toBeInTheDocument();
  });

  it('renders NO outbound link for an unsafe url (never an unsafe link)', () => {
    render(<WebSourceView citation={{ ...WEB, url: 'javascript:alert(1)' }} onClose={() => {}} />);
    expect(screen.queryByRole('link', { name: /open page/i })).not.toBeInTheDocument();
  });

  it('closes via the close button', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<WebSourceView citation={WEB} onClose={onClose} />);
    await user.click(screen.getByRole('button', { name: /close web source/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
