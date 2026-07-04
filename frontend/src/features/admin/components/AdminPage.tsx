/**
 * `/admin` — the admin console (#88/#122, ADR-0007 §4, wireframe admin.html). A
 * tenant-scoped header plus a segmented tab bar across the governance surfaces —
 * Members & roles, Model governance, LLM providers, Approvals & risk, Tool
 * governance, Sandbox governance, and Data minimization — each a self-contained
 * panel that owns its own loading / empty / error states (frontend/AGENTS.md: every
 * state, not just success). Only one panel renders at a time; the active tab's panel
 * is a `role="tabpanel"` labelled by its tab. The page body scrolls independently of
 * the pinned shell chrome (min-h-0 + contained overflow).
 *
 * The reference/governance reads (Members, Model governance, Approvals & risk, Data
 * minimization) are read-only; the write surfaces are Tool governance (#223), Sandbox
 * governance (#233), and LLM providers (per-tenant registration + model discovery).
 * The screen nests inside the shell-aware PageChrome, so the top bar / nav / account
 * menu / theme are the SHELL's; this file refines CONTENT only. Each panel reads
 * admin-only, tenant-scoped data through the api/ boundary; a non-admin caller (403,
 * INV-5) or an expired session (401, INV-4) sees an actionable per-panel error rather
 * than a blank screen.
 */
import { useState, type ReactNode } from 'react';
import { PageChrome } from '@/components/PageChrome';
import { ScrollArea } from '@/components/ScrollArea';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { AdminHeader } from './AdminHeader';
import { AdminTabs, type AdminTab } from './AdminTabs';
import { adminTabIds } from './tabIds';
import { MembersPanel } from './MembersPanel';
import { ModelGovernancePanel } from './ModelGovernancePanel';
import { RiskTierPanel } from './RiskTierPanel';
import { ToolGovernancePanel } from './ToolGovernancePanel';
import { SandboxGovernancePanel } from './SandboxGovernancePanel';
import { ProvidersPanel } from './ProvidersPanel';
import { DataMinimizationPanel } from './DataMinimizationPanel';

const TAB_PREFIX = 'admin';

type TabId = 'members' | 'models' | 'providers' | 'approvals' | 'tools' | 'sandbox' | 'data';

const TABS: AdminTab[] = [
  { id: 'members', label: 'Members & roles', icon: 'user' },
  { id: 'models', label: 'Model governance', icon: 'database' },
  { id: 'providers', label: 'LLM providers', icon: 'plug' },
  { id: 'approvals', label: 'Approvals & risk', icon: 'shield-check' },
  { id: 'tools', label: 'Tool governance', icon: 'sliders' },
  { id: 'sandbox', label: 'Sandbox governance', icon: 'shield-check' },
  { id: 'data', label: 'Data minimization', icon: 'lock' },
];

const PANELS: Record<TabId, { label: string; render: () => ReactNode }> = {
  members: { label: 'Members', render: () => <MembersPanel /> },
  models: { label: 'Model governance', render: () => <ModelGovernancePanel /> },
  providers: { label: 'LLM providers', render: () => <ProvidersPanel /> },
  approvals: { label: 'Risk tiers', render: () => <RiskTierPanel /> },
  tools: { label: 'Tool governance', render: () => <ToolGovernancePanel /> },
  sandbox: { label: 'Sandbox governance', render: () => <SandboxGovernancePanel /> },
  data: { label: 'Data minimization', render: () => <DataMinimizationPanel /> },
};

export function AdminPage() {
  const [active, setActive] = useState<TabId>('members');
  const panel = PANELS[active];
  const ids = adminTabIds(TAB_PREFIX, active);

  return (
    <PageChrome title="Admin">
      <ScrollArea viewportClassName="px-6 py-6">
        <div className="mx-auto flex max-w-4xl flex-col gap-6">
          <AdminHeader />
          <AdminTabs
            tabs={TABS}
            value={active}
            onChange={(id) => setActive(id as TabId)}
            idPrefix={TAB_PREFIX}
          />
          <div role="tabpanel" id={ids.panel} aria-labelledby={ids.tab} tabIndex={0}>
            <ErrorBoundary label={panel.label}>{panel.render()}</ErrorBoundary>
          </div>
        </div>
      </ScrollArea>
    </PageChrome>
  );
}
