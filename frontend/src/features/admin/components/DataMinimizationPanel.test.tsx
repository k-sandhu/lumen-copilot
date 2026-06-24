/**
 * DataMinimizationPanel coverage (#122): the panel is READ-ONLY (ADR-0007 §4) and
 * honest about the contract gap — the backend exposes no data-minimization policy
 * endpoint, and neither spec 0003 nor spec 0004 defines/enforces tenant-level
 * data-minimization defaults (ADR-0007 decision 5). So the panel must NOT fabricate
 * a policy list, render toggles, OR make governance/privacy promises the backend
 * cannot prove (e.g. "excluded by default", "every change audited"). It states only
 * that the policy surface is not yet exposed and is coming soon.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DataMinimizationPanel } from './DataMinimizationPanel';

describe('DataMinimizationPanel', () => {
  it('renders the data-minimization heading and the not-yet-exposed stance', () => {
    render(<DataMinimizationPanel />);
    expect(
      screen.getByRole('heading', { name: /^data minimization$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/not yet exposed by the backend/i),
    ).toBeInTheDocument();
  });

  it('is explicit that the per-policy controls are a deferred, coming-soon write surface', () => {
    render(<DataMinimizationPanel />);
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
    expect(
      screen.getByText(/not yet defined in the contract or served by the backend/i),
    ).toBeInTheDocument();
  });

  it('makes NO unsupported governance/privacy promises the backend cannot prove (ADR-0007 decision 5)', () => {
    render(<DataMinimizationPanel />);
    // These claims are not defined or enforced by spec 0003/0004 or the contract,
    // so the panel must not assert them as fact.
    expect(screen.queryByText(/excluded by default/i)).toBeNull();
    expect(screen.queryByText(/off by default/i)).toBeNull();
    expect(screen.queryByText(/sensitivity-labelled/i)).toBeNull();
    expect(screen.queryByText(/private channels/i)).toBeNull();
    expect(screen.queryByText(/every (exclusion )?change is recorded/i)).toBeNull();
    expect(screen.queryByText(/every change\s+audited/i)).toBeNull();
    expect(screen.queryByText(/core commitment/i)).toBeNull();
  });

  it('has NO mutating controls — no toggles, switches, or buttons (ADR-0007 §4)', () => {
    render(<DataMinimizationPanel />);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
