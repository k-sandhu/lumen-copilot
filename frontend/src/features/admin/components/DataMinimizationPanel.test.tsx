/**
 * DataMinimizationPanel coverage (#122): the panel is READ-ONLY (ADR-0007 §4) and
 * honest about the contract gap — the backend exposes no data-minimization policy
 * endpoint, so the panel must NOT fabricate a policy list or render toggles. It
 * states the data-minimization principle and is explicit that the per-policy
 * controls are a deferred write surface.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DataMinimizationPanel } from './DataMinimizationPanel';

describe('DataMinimizationPanel', () => {
  it('renders the data-minimization heading and principle', () => {
    render(<DataMinimizationPanel />);
    expect(
      screen.getByRole('heading', { name: /^data minimization$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/sources are off by default/i)).toBeInTheDocument();
    expect(screen.getByText(/every exclusion change is recorded/i)).toBeInTheDocument();
  });

  it('is explicit that the per-policy controls are a deferred write surface', () => {
    render(<DataMinimizationPanel />);
    expect(screen.getByText(/not yet exposed by the backend/i)).toBeInTheDocument();
  });

  it('has NO mutating controls — no toggles, switches, or buttons (ADR-0007 §4)', () => {
    render(<DataMinimizationPanel />);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
