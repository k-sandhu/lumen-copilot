/**
 * Tests for the audit KPI tiles (#121). The row shows exactly three tiles
 * (events / access-denied / answers-cited) — never a faked "Avg latency" — and
 * an em-dash grounding rate when there are no answers on the page.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AuditKpis } from './AuditKpis';
import type { AuditMetrics } from '../model/metrics';

const metrics: AuditMetrics = {
  total: 12,
  denied: 2,
  answers: 5,
  answersCited: 5,
  citedRate: 1,
};

describe('AuditKpis', () => {
  it('renders the three honest tiles and no latency tile', () => {
    render(<AuditKpis metrics={metrics} />);
    expect(screen.getByRole('group', { name: /events \(this page\)/i })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: /access denied/i })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: /answers cited/i })).toBeInTheDocument();
    expect(screen.queryByText(/latency/i)).not.toBeInTheDocument();
  });

  it('shows the cited percentage and the supporting count', () => {
    render(<AuditKpis metrics={metrics} />);
    expect(screen.getByText('100%')).toBeInTheDocument();
    expect(screen.getByText(/5 of 5 answers/i)).toBeInTheDocument();
  });

  it('renders an em-dash grounding rate when there are no answers', () => {
    render(
      <AuditKpis metrics={{ total: 3, denied: 0, answers: 0, answersCited: 0, citedRate: null }} />,
    );
    const group = screen.getByRole('group', { name: /answers cited/i });
    expect(group).toHaveTextContent('—');
  });

  it('renders skeleton tiles while loading', () => {
    const { container } = render(<AuditKpis metrics={metrics} loading />);
    expect(container.querySelectorAll('.lc-skeleton').length).toBeGreaterThan(0);
  });
});
