/**
 * Form model for the assistant editor (#212) — the client-side draft state and
 * the pure mappers between it and the wire shapes (AssistantCreate /
 * AssistantUpdate). Kept out of the component so it is unit-testable and the JSX
 * stays presentational (frontend/AGENTS.md: no business logic in JSX).
 *
 * Design: the form holds every editable field as a concrete value (model/owner
 * are '' when unset, not null) so the inputs are controlled; the mappers convert
 * '' back to the wire's null/omit at submit time.
 */
import type {
  Assistant,
  AssistantCreate,
  AssistantUpdate,
  AutonomyLevel,
  KnowledgeScope,
} from '@/api';

/** The editable draft the form binds its inputs to. */
export interface AssistantFormState {
  name: string;
  description: string;
  instructions: string;
  /** '' ⇒ smart server default (null on the wire). */
  model: string;
  knowledgeScope: KnowledgeScope;
  toolAllowlist: string[];
  autonomyLevel: AutonomyLevel;
  /** '' ⇒ unset (owner is server-assigned at create; editable after). */
  owner: string;
  /** '' ⇒ no backup owner set. */
  backupOwner: string;
}

/** A fresh draft for `/assistants/new`. */
export function emptyForm(): AssistantFormState {
  return {
    name: '',
    description: '',
    instructions: '',
    model: '',
    knowledgeScope: { collectionIds: [], sourceIds: [], modes: [] },
    toolAllowlist: [],
    autonomyLevel: 'suggest',
    owner: '',
    backupOwner: '',
  };
}

/** Hydrate the form from an existing assistant (edit mode). */
export function formFromAssistant(a: Assistant): AssistantFormState {
  return {
    name: a.name,
    description: a.description ?? '',
    instructions: a.instructions ?? '',
    model: a.model ?? '',
    knowledgeScope: {
      collectionIds: a.knowledgeScope.collectionIds ?? [],
      sourceIds: a.knowledgeScope.sourceIds ?? [],
      modes: a.knowledgeScope.modes ?? [],
    },
    toolAllowlist: a.toolAllowlist,
    autonomyLevel: a.autonomyLevel,
    owner: a.owner,
    backupOwner: a.backupOwner ?? '',
  };
}

/**
 * The create body (POST /assistants). Only `name` is required; everything else is
 * sent when meaningfully set. `owner` is server-assigned on create, so it is NOT
 * part of the create body (it's set via a follow-up PATCH once known).
 */
export function toCreateBody(form: AssistantFormState): AssistantCreate {
  const body: AssistantCreate = { name: form.name.trim() };
  if (form.description.trim()) body.description = form.description.trim();
  if (form.instructions.trim()) body.instructions = form.instructions.trim();
  if (form.model) body.model = form.model;
  if (hasScope(form.knowledgeScope)) body.knowledgeScope = form.knowledgeScope;
  if (form.toolAllowlist.length > 0) body.toolAllowlist = form.toolAllowlist;
  body.autonomyLevel = form.autonomyLevel;
  if (form.backupOwner) body.backupOwner = form.backupOwner;
  return body;
}

/** The full update body (PATCH /assistants/{id}) — sends the whole head. */
export function toUpdateBody(form: AssistantFormState): AssistantUpdate {
  return {
    name: form.name.trim(),
    description: form.description.trim() ? form.description.trim() : null,
    instructions: form.instructions.trim() ? form.instructions.trim() : null,
    model: form.model ? form.model : null,
    knowledgeScope: form.knowledgeScope,
    toolAllowlist: form.toolAllowlist,
    autonomyLevel: form.autonomyLevel,
    backupOwner: form.backupOwner ? form.backupOwner : null,
  };
}

function hasScope(scope: KnowledgeScope): boolean {
  return (
    (scope.collectionIds?.length ?? 0) > 0 ||
    (scope.sourceIds?.length ?? 0) > 0 ||
    (scope.modes?.length ?? 0) > 0
  );
}

/**
 * Whether the assistant can be published from the UI (ADR-0011 §4): both an owner
 * and a DISTINCT backup owner must be set, and it must have a name. The server is
 * authoritative (a bad publish → 422), but gating here keeps the button honest.
 */
export function canPublish(form: AssistantFormState): boolean {
  return (
    form.name.trim().length > 0 &&
    form.owner.trim().length > 0 &&
    form.backupOwner.trim().length > 0 &&
    form.owner !== form.backupOwner
  );
}
