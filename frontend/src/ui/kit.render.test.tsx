/**
 * Kit integration smoke (issue #81 AC): the trust kit mounts cleanly under BOTH
 * [data-mode="light"] and [data-mode="dark"]. The kit is token-driven, so the
 * same markup re-skins purely from the mode attribute on a host element — there
 * is no per-mode branching in the components. (Vitest runs with css:false, so
 * this asserts structural correctness under each mode, not computed color.)
 */
import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { applyAppearance } from './theme/mode';
import {
  PermissionPill,
  FreshnessPill,
  StatusDot,
  RiskTierBadge,
  KpiCard,
  CitationChip,
  SourceInspector,
  RetrievalTrace,
  AuditRow,
  type AuditEvent,
} from './index';

const EVENT: AuditEvent = { id: 'evt_1', kind: 'retrieval', action: 'Retrieved 6 passages' };

function Kit() {
  return (
    <div className="lc-kit" data-testid="kit">
      <PermissionPill level="restricted" label="Restricted · masked fields" />
      <FreshnessPill label="Indexed 1h ago" />
      <StatusDot tone="sync" label="Syncing" />
      <RiskTierBadge tier="T2" />
      <KpiCard label="Retrievals" value="1,204" delta="+5%" trend="up" />
      <CitationChip index={1} />
      <SourceInspector
        title="Doc.pdf"
        passage={{ runs: [{ text: 'hello ' }, { text: 'world', highlight: true }] }}
        owner="Dana"
        freshness="2h ago"
        permission="granted"
      />
      <RetrievalTrace summary="3 sources" steps={[{ label: 'Searched' }]} defaultOpen />
      <AuditRow event={EVENT} />
    </div>
  );
}

describe('trust kit renders in both modes', () => {
  it.each(['light', 'dark'] as const)('mounts under [data-mode="%s"]', (mode) => {
    applyAppearance(mode);
    expect(document.documentElement.getAttribute('data-mode')).toBe(mode);

    render(<Kit />);
    const kit = screen.getByTestId('kit');
    // Every component is present and structurally intact in this mode. The
    // standalone permission pill is "restricted"; the SourceInspector renders
    // its own "granted" pill, so this name is unambiguous.
    expect(
      within(kit).getByRole('status', { name: /restricted · masked fields/i }),
    ).toBeInTheDocument();
    expect(within(kit).getByText('Indexed 1h ago')).toBeInTheDocument();
    expect(within(kit).getByText('Syncing')).toBeInTheDocument();
    expect(within(kit).getByLabelText(/risk tier T2/i)).toBeInTheDocument();
    expect(within(kit).getByText('1,204')).toBeInTheDocument();
    expect(within(kit).getByRole('button', { name: /citation 1/i })).toBeInTheDocument();
    expect(within(kit).getByText('world').tagName.toLowerCase()).toBe('mark');
    expect(within(kit).getByText('Searched')).toBeInTheDocument();
    expect(within(kit).getByText('Retrieved 6 passages')).toBeInTheDocument();
  });
});
