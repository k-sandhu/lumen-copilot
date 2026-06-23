import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuditRow, type AuditEvent } from './AuditRow';

const EVENT: AuditEvent = {
  id: 'evt_9f3a',
  kind: 'answer',
  action: 'Answered: Q3 revenue by region',
  detail: 'dana@acme · Assistant',
  time: '14:02:11',
};

describe('AuditRow', () => {
  it('renders the id, action, detail, and time', () => {
    render(<AuditRow event={EVENT} />);
    expect(screen.getByText('evt_9f3a')).toBeInTheDocument();
    expect(screen.getByText('Answered: Q3 revenue by region')).toBeInTheDocument();
    expect(screen.getByText('dana@acme · Assistant')).toBeInTheDocument();
    expect(screen.getByText('14:02:11')).toBeInTheDocument();
  });

  it('is an accessible button naming the event', () => {
    render(<AuditRow event={EVENT} />);
    expect(
      screen.getByRole('button', { name: /audit event evt_9f3a: answered/i }),
    ).toBeInTheDocument();
  });

  it('calls onSelect with the event when activated', async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<AuditRow event={EVENT} onSelect={onSelect} />);
    await user.click(screen.getByRole('button'));
    expect(onSelect).toHaveBeenCalledWith(EVENT);
  });
});
