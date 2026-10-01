import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CitationChip } from './CitationChip';

describe('CitationChip', () => {
  it('renders the citation index inside a button', () => {
    render(<CitationChip index={3} />);
    expect(screen.getByRole('button', { name: /citation 3/i })).toHaveTextContent('3');
  });

  it('includes the source title in its accessible name', () => {
    render(<CitationChip index={1} sourceTitle="Q3 Revenue.pdf" />);
    expect(screen.getByRole('button', { name: 'Citation 1: Q3 Revenue.pdf' })).toBeInTheDocument();
  });

  it('reflects the open state via aria-pressed', () => {
    const { rerender } = render(<CitationChip index={2} active={false} />);
    expect(screen.getByRole('button')).toHaveAttribute('aria-pressed', 'false');
    rerender(<CitationChip index={2} active />);
    expect(screen.getByRole('button')).toHaveAttribute('aria-pressed', 'true');
  });

  it('invokes onClick when activated', async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(<CitationChip index={1} onClick={onClick} />);
    await user.click(screen.getByRole('button'));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it('shows a media timestamp in both visible and accessible labels', () => {
    render(<CitationChip index={2} sourceTitle="meeting.mp4" timeStartMs={72_500} />);
    const chip = screen.getByRole('button', { name: 'Citation 2: meeting.mp4 at 1:12' });
    expect(chip).toHaveTextContent('2· 1:12');
  });
});
