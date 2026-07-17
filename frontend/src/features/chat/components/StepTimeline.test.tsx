/**
 * Live run-phase timeline (spec 0006 #429, AC-1): running steps pulse, completed
 * steps check off; labels are server-supplied (unknown keys render fine);
 * turn/detail annotations render when present.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StepTimeline } from './StepTimeline';
import type { ChatStep } from '@/api';

const STEPS: ChatStep[] = [
  { key: 'prepare', label: 'Preparing', state: 'completed' },
  { key: 'think', label: 'Thinking', state: 'started', turn: 2, detail: 'requested 1 tool' },
  { key: 'custom_future_phase', label: 'Reticulating splines', state: 'started' },
];

describe('StepTimeline', () => {
  it('renders each step with its state and annotations (AC-1)', () => {
    render(<StepTimeline steps={STEPS} />);
    const list = screen.getByRole('list', { name: 'Answer progress' });
    expect(list).toBeInTheDocument();
    expect(screen.getByText('Preparing')).toBeInTheDocument();
    expect(screen.getByText(/Thinking \(turn 2\) — requested 1 tool/)).toBeInTheDocument();
    // Unknown keys render generically off the server label (forward-compatible).
    expect(screen.getByText('Reticulating splines')).toBeInTheDocument();
  });

  it('renders nothing for an empty step list', () => {
    render(<StepTimeline steps={[]} />);
    expect(screen.queryByRole('list', { name: 'Answer progress' })).not.toBeInTheDocument();
  });
});
