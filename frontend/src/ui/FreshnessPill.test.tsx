import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FreshnessPill } from './FreshnessPill';

describe('FreshnessPill', () => {
  it('renders the recency label', () => {
    render(<FreshnessPill label="Indexed 2h ago" />);
    expect(screen.getByText('Indexed 2h ago')).toBeInTheDocument();
  });

  it('is not stale by default', () => {
    const { container } = render(<FreshnessPill label="Updated just now" />);
    expect(container.querySelector('.lc-fresh')).not.toHaveClass('lc-fresh--stale');
  });

  it('marks the stale variant', () => {
    const { container } = render(<FreshnessPill label="Indexed 40d ago" stale />);
    expect(container.querySelector('.lc-fresh')).toHaveClass('lc-fresh--stale');
  });

  it('exposes a machine timestamp through the accessible name', () => {
    render(<FreshnessPill label="2h ago" title="2026-06-23T12:00:00Z" />);
    expect(screen.getByLabelText('2h ago (2026-06-23T12:00:00Z)')).toBeInTheDocument();
  });
});
