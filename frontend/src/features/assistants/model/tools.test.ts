/**
 * The builder's tool-option projection (#505, and the review findings against it).
 *
 * The interesting case is an ORPHAN: a name already in the assistant's allow-list that
 * the member-readable catalogue does not describe. `mcp:*` grants are the real-world
 * instance — the backend validates them against the tenant's MCP servers, so they are
 * legitimate grants, not junk. The first cut synthesised those rows as
 * `T0 + readOnly + enabled:false`, which made a side-effecting MCP tool render as a
 * harmless read-only tool that "an admin turned off" (none did) and, because
 * `locked = unavailable && !checked`, made it impossible to re-tick once removed.
 */
import { describe, expect, it } from 'vitest';
import type { ToolCatalogEntry } from '@/api';
import { toToolOption, toToolOptions } from './tools';

function entry(over: Partial<ToolCatalogEntry> = {}): ToolCatalogEntry {
  return {
    tool_name: 'search_text',
    description: 'Search the corpus.',
    risk_tier: 'T0',
    read_only: true,
    enabled: true,
    requires_approval: false,
    ...over,
  } as ToolCatalogEntry;
}

describe('toToolOption (catalogue entries)', () => {
  it('carries the catalogue’s governance verbatim and marks the row as catalogued', () => {
    const option = toToolOption(
      entry({
        tool_name: 'run_python',
        risk_tier: 'T2',
        read_only: false,
        requires_approval: true,
      }),
    );
    expect(option).toMatchObject({
      name: 'run_python',
      riskTier: 'T2',
      readOnly: false,
      requiresApproval: true,
      catalogued: true,
    });
  });
});

describe('toToolOptions (orphans — the review finding)', () => {
  const orphanOf = (selected: string[]) =>
    toToolOptions([entry()], selected).find((o) => o.name === selected[0]);

  it('keeps an already-granted name that the catalogue does not describe', () => {
    const names = toToolOptions([entry()], ['mcp:jira:create_issue']).map((o) => o.name);
    // Dropping it silently would rewrite the owner's config on the next save.
    expect(names).toContain('mcp:jira:create_issue');
  });

  it('marks it uncatalogued rather than claiming governance it does not know', () => {
    expect(orphanOf(['mcp:jira:create_issue'])?.catalogued).toBe(false);
  });

  it('does NOT claim an admin disabled it (enabled stays true, so the row is not locked)', () => {
    // `enabled:false` drove BOTH the false "an admin has turned this tool off" copy and
    // `locked`, which permanently barred re-ticking a still-granted MCP tool.
    expect(orphanOf(['mcp:jira:create_issue'])?.enabled).toBe(true);
  });

  it('does NOT claim it is read-only — unknown risk is treated as side-effecting', () => {
    // The conservative direction: an unknown tool keeps the "this tool acts" warning
    // instead of having it suppressed by a made-up `readOnly: true`.
    expect(orphanOf(['mcp:jira:create_issue'])?.readOnly).toBe(false);
  });

  it('does not invent an approval requirement for it', () => {
    expect(orphanOf(['mcp:jira:create_issue'])?.requiresApproval).toBe(false);
  });

  it('leaves catalogued entries untouched when an orphan is present', () => {
    const options = toToolOptions([entry()], ['mcp:x']);
    expect(options.find((o) => o.name === 'search_text')).toMatchObject({
      catalogued: true,
      readOnly: true,
      enabled: true,
    });
  });

  it('produces no orphan when every selected name is catalogued', () => {
    expect(toToolOptions([entry()], ['search_text'])).toHaveLength(1);
  });
});
