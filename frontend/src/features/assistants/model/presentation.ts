/**
 * Pure display helpers for the assistant slice (#212) — no I/O, no React, so they
 * are trivially unit-testable and shared across the library + editor. Everything
 * here maps wire shapes (contracts/openapi.yaml §assistants) to the labels/tones
 * the UI renders.
 */
import type { StatusTone } from '@/components/StatusBadge';
import type {
  AssistantStatus,
  AutonomyLevel,
  ChatModelInfo,
  KnowledgeMode,
  Member,
} from '@/api';

/** Human labels for the lifecycle status. */
export const STATUS_LABEL: Record<AssistantStatus, string> = {
  draft: 'Draft',
  published: 'Published',
  disabled: 'Disabled',
};

/** Status → StatusBadge tone (published = ok, draft = pending, disabled = danger). */
export function statusTone(status: AssistantStatus): StatusTone {
  switch (status) {
    case 'published':
      return 'ok';
    case 'disabled':
      return 'danger';
    case 'draft':
    default:
      return 'pending';
  }
}

/** Ordered autonomy levels with human labels + a one-line meaning (ADR-0011 §3). */
export const AUTONOMY_OPTIONS: ReadonlyArray<{
  value: AutonomyLevel;
  label: string;
  hint: string;
}> = [
  { value: 'suggest', label: 'Suggest', hint: 'Proposes answers only — never acts.' },
  { value: 'draft', label: 'Draft', hint: 'Drafts content for a human to send.' },
  {
    value: 'act_with_approval',
    label: 'Act with approval',
    hint: 'May take actions, each gated on explicit approval.',
  },
  {
    value: 'act_auto',
    label: 'Act automatically',
    hint: 'Acts without per-action approval (capped by tool risk tier).',
  },
];

export function autonomyLabel(level: AutonomyLevel): string {
  return AUTONOMY_OPTIONS.find((o) => o.value === level)?.label ?? level;
}

/** The knowledge-scope source classes, with labels (ADR-0011 §1/§2). */
export const KNOWLEDGE_MODE_OPTIONS: ReadonlyArray<{
  value: KnowledgeMode;
  label: string;
  hint: string;
}> = [
  { value: 'company', label: 'Company sources', hint: 'Connected enterprise sources.' },
  { value: 'uploaded', label: 'Uploaded files', hint: 'Documents you uploaded.' },
  { value: 'web', label: 'Web', hint: 'Public web pages the assistant may read.' },
  { value: 'model', label: 'Model knowledge', hint: 'The model’s own training knowledge.' },
];

/**
 * The model label for a card/summary. `null` ⇒ the smart server default resolved
 * at run time; an unknown id (not in the registry) is shown verbatim.
 */
export function modelLabel(
  modelId: string | null | undefined,
  models: ChatModelInfo[] | undefined,
): string {
  if (modelId == null) return 'Smart default';
  return models?.find((m) => m.id === modelId)?.label ?? modelId;
}

/**
 * The owner's display label for a card. Falls back to the raw id when the member
 * roster hasn't loaded or the owner isn't in it (e.g. a non-admin caller can't
 * read `/admin/members`, so we degrade gracefully rather than blank the field).
 */
export function ownerLabel(ownerId: string, members: Member[] | undefined): string {
  return members?.find((m) => m.id === ownerId)?.email ?? shortId(ownerId);
}

/** A compact id for when no human label is available (never a blank pane). */
export function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

/**
 * Format an ISO timestamp as a short local datetime for the version-history panel
 * (#214), or an em-dash placeholder for a missing/invalid value (never a blank
 * cell). Locale-driven; not faked when absent.
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
