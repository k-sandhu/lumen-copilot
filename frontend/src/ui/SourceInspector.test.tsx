import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SourceInspector, type SourcePassage } from './SourceInspector';

const PASSAGE: SourcePassage = {
  runs: [
    { text: 'Revenue grew ' },
    { text: '18% in Q3', highlight: true },
    { text: ' across all regions.' },
  ],
};

describe('SourceInspector', () => {
  it('renders the passage with the matched span highlighted in a <mark>', () => {
    const { container } = render(<SourceInspector title="Q3 Revenue.pdf" passage={PASSAGE} />);
    const mark = container.querySelector('mark');
    expect(mark).toHaveTextContent('18% in Q3');
    expect(screen.getByText(/across all regions/)).toBeInTheDocument();
  });

  it('shows owner, freshness, and the permission pill', () => {
    render(
      <SourceInspector
        title="Q3 Revenue.pdf"
        passage={PASSAGE}
        owner="Dana Ruiz"
        freshness="Indexed 2h ago"
        permission="granted"
      />,
    );
    expect(screen.getByText('Dana Ruiz')).toBeInTheDocument();
    expect(screen.getByText('Indexed 2h ago')).toBeInTheDocument();
    expect(screen.getByRole('status', { name: /you have access/i })).toBeInTheDocument();
  });

  it('renders an "Open in source" deep link when href is given', () => {
    render(<SourceInspector title="Doc" passage={PASSAGE} href="https://example.com/doc" />);
    expect(screen.getByRole('link', { name: /open in source/i })).toHaveAttribute(
      'href',
      'https://example.com/doc',
    );
  });

  it('shows the loading state', () => {
    const { container } = render(<SourceInspector title="Doc" loading />);
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
    expect(container.querySelectorAll('.lc-skeleton').length).toBeGreaterThan(0);
  });

  it('shows an actionable error state with retry', async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    render(<SourceInspector title="Doc" error="Could not load passage." onRetry={onRetry} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load passage.');
    await user.click(screen.getByRole('button', { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('shows the empty state when no source is selected', () => {
    render(<SourceInspector />);
    expect(screen.getByText(/select a citation/i)).toBeInTheDocument();
  });

  it('fires onClose from the header close button', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<SourceInspector title="Doc" passage={PASSAGE} onClose={onClose} />);
    await user.click(screen.getByRole('button', { name: /close source inspector/i }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('renders the web variant: host chip + "Open page" link (#221)', () => {
    render(
      <SourceInspector
        title="A web page"
        passage={PASSAGE}
        web={{ host: 'example.org' }}
        href="https://example.org/page"
      />,
    );
    // Host is shown as a distinct meta chip, and the outbound link reads
    // "Open page" (not "Open in source") and is safe.
    expect(screen.getByText('example.org')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /open page/i });
    expect(link).toHaveAttribute('href', 'https://example.org/page');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByRole('link', { name: /open in source/i })).not.toBeInTheDocument();
  });
});
