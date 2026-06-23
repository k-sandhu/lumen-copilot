/**
 * Tests for the client-side CSV export (#121). It's disabled with nothing to
 * export, and on click it serializes the visible events to a CSV blob and
 * triggers a download — entirely in the browser, no backend call.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AuditEvent } from '@/api';
import { ExportButton } from './ExportButton';

const EVENT: AuditEvent = {
  id: 'evt_1',
  ts: '2026-06-19T10:00:00Z',
  actor: 'dana@acme',
  tenant_id: 't1',
  event_type: 'answer.generated',
  resource_id: 'doc-9',
  decision: 'allowed',
  provenance: { candidates: [{ resource_id: 'p1', disposition: 'allow', reason: 'rank 1' }] },
};

describe('ExportButton', () => {
  let createObjectURL: ReturnType<typeof vi.fn>;
  let revokeObjectURL: ReturnType<typeof vi.fn>;
  let clickSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    createObjectURL = vi.fn(() => 'blob:mock');
    revokeObjectURL = vi.fn();
    // jsdom doesn't implement object URLs.
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true });
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true });
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  });

  afterEach(() => {
    clickSpy.mockRestore();
  });

  it('is disabled when there is nothing to export', () => {
    render(<ExportButton events={[]} />);
    expect(screen.getByRole('button', { name: /export csv/i })).toBeDisabled();
  });

  it('triggers a CSV download of the current events on click', async () => {
    const user = userEvent.setup();
    render(<ExportButton events={[EVENT]} />);
    await user.click(screen.getByRole('button', { name: /export csv/i }));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0]?.[0] as Blob;
    expect(blob.type).toContain('text/csv');
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock');
  });
});
