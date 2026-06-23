import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RiskTierBadge } from './RiskTierBadge';

describe('RiskTierBadge', () => {
  it('renders the tier code and default policy label', () => {
    render(<RiskTierBadge tier="T0" />);
    expect(screen.getByText('T0')).toBeInTheDocument();
    expect(screen.getByText('Auto')).toBeInTheDocument();
  });

  it('escalates color with the tier (T3 = danger, dual approval)', () => {
    const { container } = render(<RiskTierBadge tier="T3" />);
    expect(container.querySelector('.lc-badge')).toHaveClass('lc-badge--danger');
    expect(screen.getByText('Dual approval')).toBeInTheDocument();
  });

  it('exposes tier + policy through an accessible label', () => {
    render(<RiskTierBadge tier="T2" />);
    expect(screen.getByLabelText('Risk tier T2: Approval')).toBeInTheDocument();
  });

  it('can hide the policy label and accept an override', () => {
    const { rerender } = render(<RiskTierBadge tier="T1" showLabel={false} />);
    expect(screen.queryByText('Notify')).not.toBeInTheDocument();
    rerender(<RiskTierBadge tier="T1" label="Logged" />);
    expect(screen.getByText('Logged')).toBeInTheDocument();
  });
});
