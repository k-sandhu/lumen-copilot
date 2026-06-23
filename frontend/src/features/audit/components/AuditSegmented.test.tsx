/**
 * Tests for the client-side segmented event filter (#121) — labelled toggle
 * group, exactly one pressed segment, per-segment counts, and change events.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuditSegmented } from './AuditSegmented';
import type { AuditSegment } from '../model/metrics';

const counts: Record<AuditSegment, number> = {
  all: 10,
  retrieval: 4,
  answer: 3,
  action: 2,
  access: 1,
};

describe('AuditSegmented', () => {
  it('renders a labelled group with all five segments', () => {
    render(<AuditSegmented value="all" onChange={vi.fn()} counts={counts} />);
    const group = screen.getByRole('group', { name: /filter events by type/i });
    expect(group).toBeInTheDocument();
    for (const label of ['All', 'Retrieval', 'Answer', 'Action', 'Access denied']) {
      expect(screen.getByRole('button', { name: new RegExp(label, 'i') })).toBeInTheDocument();
    }
  });

  it('marks exactly the active segment as pressed', () => {
    render(<AuditSegmented value="answer" onChange={vi.fn()} counts={counts} />);
    expect(screen.getByRole('button', { name: /answer/i })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /^all/i })).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows per-segment counts', () => {
    render(<AuditSegmented value="all" onChange={vi.fn()} counts={counts} />);
    expect(screen.getByRole('button', { name: /retrieval 4/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /access denied 1/i })).toBeInTheDocument();
  });

  it('emits the chosen segment on click', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<AuditSegmented value="all" onChange={onChange} counts={counts} />);
    await user.click(screen.getByRole('button', { name: /retrieval/i }));
    expect(onChange).toHaveBeenCalledWith('retrieval');
  });
});
