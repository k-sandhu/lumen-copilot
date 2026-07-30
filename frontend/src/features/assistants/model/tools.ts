/**
 * The tool allow-list source for the assistant builder (#212 → #505, ADR-0011 §1).
 *
 * The candidate tools now come from the SERVER (`GET /tools`, the member-readable
 * tool catalogue added in #505), not from a hardcoded stub. The old `KNOWN_TOOLS`
 * constant listed four retrieval tools and nothing else, so `run_python` — a real,
 * registered, T2 tool the backend has always accepted in `toolAllowlist` — was
 * simply unreachable from the UI: the only assistant that had it was seeded straight
 * into the database.
 *
 * What stays local is pure PRESENTATION: a friendly label per known tool name, and
 * the risk copy that makes a side-effecting tool legible. Governance is never
 * decided here — `enabled` / `requires_approval` / `risk_tier` all arrive from the
 * catalogue, which reflects the tenant's admin policy.
 *
 * An empty allow-list on the wire (`[]`) still means "the default retrieval tool
 * set" (per the contract), so an assistant with nothing ticked is still useful.
 */
import type { RiskTierId, ToolCatalogEntry } from '@/api';

/** One selectable tool: the catalogue entry plus the copy the checkbox renders. */
export interface ToolOption {
  /** The wire name persisted into `toolAllowlist`. */
  name: string;
  label: string;
  description: string;
  riskTier: RiskTierId;
  /** False for a side-effecting (write-tier) tool — surfaced, never hidden. */
  readOnly: boolean;
  /**
   * False when the tenant's admin has switched the tool off. Such a tool is shown
   * as UNAVAILABLE rather than offered as a control the run-time gate would refuse.
   */
  enabled: boolean;
  /** True when an invocation is still approval-gated for this tenant. */
  requiresApproval: boolean;
  /**
   * False for a name already in the allow-list that the catalogue does not describe
   * (a retired registry tool, or an `mcp:*` tool granted elsewhere — those are
   * validated server-side against the tenant's MCP servers, so they are LEGITIMATE
   * grants, not junk). Such a row has NO authoritative governance metadata, so the
   * other fields on it are placeholders and must not be presented as facts: it gets
   * its own conservative copy rather than borrowing the admin-disabled or T0
   * read-only presentation, either of which would state something untrue.
   */
  catalogued: boolean;
}

/**
 * Friendly labels for the tools we ship. A name with no entry here falls back to its
 * wire name, so a newly registered backend tool appears in the picker immediately
 * (with its registry description) instead of being invisible until the UI catches up
 * — the failure mode #505 is about.
 */
const LABELS: Record<string, string> = {
  search_text: 'Text search',
  search_documents: 'Document search',
  list_documents: 'List documents',
  get_document: 'Read document',
  web_search: 'Web search',
  write_file: 'Write a file',
  run_python: 'Run Python (code execution)',
};

/** A human label for a tool name; falls back to the wire name itself. */
export function toolLabel(name: string): string {
  return LABELS[name] ?? name;
}

/** Project a catalogue entry into the option the picker renders. */
export function toToolOption(entry: ToolCatalogEntry): ToolOption {
  return {
    name: entry.tool_name,
    label: toolLabel(entry.tool_name),
    description: entry.description,
    riskTier: entry.risk_tier,
    readOnly: entry.read_only,
    enabled: entry.enabled,
    requiresApproval: entry.requires_approval,
    catalogued: true,
  };
}

/**
 * The picker's rows: every catalogue entry, plus a placeholder for any name already
 * in the allow-list that the catalogue does not describe (a tool retired from the
 * registry, or an `mcp:*` tool granted elsewhere). Such a row is rendered so the
 * owner can SEE and remove it — dropping it silently would rewrite their config
 * behind their back on the next save.
 */
export function toToolOptions(entries: ToolCatalogEntry[], selected: string[]): ToolOption[] {
  const options = entries.map(toToolOption);
  const known = new Set(options.map((o) => o.name));
  const orphans = selected
    .filter((name) => !known.has(name))
    .map<ToolOption>((name) => ({
      name,
      label: toolLabel(name),
      description:
        'Already granted, but not described by this tenant’s tool catalogue. Its risk and' +
        ' governance are unknown here, so it is shown conservatively.',
      // Placeholders ONLY — `catalogued: false` is what the renderer branches on.
      // Deliberately NOT `T0` + `readOnly: true` + `enabled: false`: that trio made an
      // `mcp:*` grant render as a harmless read-only tool that "an admin turned off"
      // (no admin did), suppressing the side-effecting warning on a component whose
      // contract is "governance is displayed, never bypassed". `readOnly: false` keeps
      // the conservative side-effecting copy; `enabled: true` keeps the row unlocked so
      // a still-granted tool can be re-ticked after an accidental untick.
      riskTier: 'T0',
      readOnly: false,
      enabled: true,
      requiresApproval: false,
      catalogued: false,
    }));
  return [...options, ...orphans];
}
