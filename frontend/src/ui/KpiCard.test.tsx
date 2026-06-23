import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { KpiCard } from './KpiCard';

describe('KpiCard', () => {
  it('renders label + value as an accessible group', () => {
    render(<KpiCard label="Retrievals today" value="1,204" />);
    const group = screen.getByRole('group', { name: 'Retrievals today' });
    expect(group).toBeInTheDocument();
    expect(screen.getByText('1,204')).toBeInTheDocument();
  });

  it('shows an up trend delta with the trending-up direction', () => {
    const { container } = render(
      <KpiCard label="Answers" value="98%" delta="+4% WoW" trend="up" />,
    );
    expect(screen.getByText('+4% WoW')).toBeInTheDocument();
    expect(container.querySelector('.lc-kpi__delta')).toHaveClass('lc-kpi__delta--up');
  });

  it('renders a skeleton and hides the delta while loading', () => {
    const { container } = render(
      <KpiCard label="Latency" value="42ms" delta="-3ms" trend="down" loading />,
    );
    expect(container.querySelector('.lc-skeleton')).toBeInTheDocument();
    expect(screen.queryByText('42ms')).not.toBeInTheDocument();
    expect(screen.queryByText('-3ms')).not.toBeInTheDocument();
  });

  it('renders an em-dash for an empty value', () => {
    render(<KpiCard label="Pending" />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
