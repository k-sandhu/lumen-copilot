import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StatusDot } from './StatusDot';

describe('StatusDot', () => {
  it('renders a label and the matching tone class', () => {
    const { container } = render(<StatusDot tone="ok" label="Synced" />);
    expect(screen.getByText('Synced')).toBeInTheDocument();
    expect(container.querySelector('.lc-dot')).toHaveClass('lc-dot--ok');
  });

  it('uses the sync tone (pulsing dot) for live activity', () => {
    const { container } = render(<StatusDot tone="sync" label="Syncing" />);
    expect(container.querySelector('.lc-dot')).toHaveClass('lc-dot--sync');
  });

  it('falls back to the label for its accessible name', () => {
    render(<StatusDot tone="warn" label="Degraded" />);
    expect(screen.getByRole('status', { name: 'Degraded' })).toBeInTheDocument();
  });

  it('supports a standalone (label-less) dot with a title for a11y', () => {
    render(<StatusDot tone="danger" title="Connection lost" />);
    expect(screen.getByRole('status', { name: 'Connection lost' })).toBeInTheDocument();
  });
});
