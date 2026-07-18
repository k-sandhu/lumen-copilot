/**
 * Knowledge-mode config + availability logic (#221, epic E3-12) — the non-React
 * half of the composer's knowledge-mode control, kept out of the component file
 * so it is unit-testable and so the component file only exports a component
 * (react-refresh). These describe the four wire modes (`KnowledgeMode`,
 * contracts/openapi.yaml §assistants) the UI can surface.
 */
import type { IconName } from '@/ui';
import type { KnowledgeMode } from '@/api';

export interface ModeConfig {
  label: string;
  icon: IconName;
  hint: string;
}

export const MODE_CONFIG: Record<KnowledgeMode, ModeConfig> = {
  company: {
    label: 'Company',
    icon: 'database',
    hint: 'Answer from connected enterprise sources you can access',
  },
  uploaded: {
    label: 'Uploaded',
    icon: 'file-text',
    hint: 'Answer from documents you uploaded',
  },
  web: {
    label: 'Web',
    icon: 'globe',
    hint: 'Let the assistant search public web pages',
  },
  model: {
    label: 'Model',
    icon: 'sparkles',
    hint: 'Allow the model to answer from its own training knowledge',
  },
};

/** Render order — company/uploaded (corpus) first, then web, then model. */
export const MODE_ORDER: readonly KnowledgeMode[] = [
  'company',
  'uploaded',
  'web',
  'model',
] as const;

/** Whether a mode may be toggled at all, and why not when it can't. */
export interface ModeAvailability {
  available: boolean;
  /** Shown (tooltip + a11y description) when `available` is false. */
  reason?: string;
}

/**
 * Resolve a mode's availability. Web fails closed: absent ⇒ unavailable with a
 * default reason, so a deploy/assistant that never enabled web shows a disabled
 * toggle rather than a working-looking one. Every other mode is available unless
 * a caller explicitly marks it unavailable.
 */
export function resolveAvailability(
  mode: KnowledgeMode,
  availability: Partial<Record<KnowledgeMode, ModeAvailability>> | undefined,
): ModeAvailability {
  const entry = availability?.[mode];
  if (entry) return entry;
  if (mode === 'web') {
    return { available: false, reason: 'Web search is off for this workspace.' };
  }
  return { available: true };
}
