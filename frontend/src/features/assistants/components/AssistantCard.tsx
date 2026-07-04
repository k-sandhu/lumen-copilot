/**
 * AssistantCard (#212) — one assistant in the library grid. Shows name, owner,
 * model, lifecycle status, and tool badges, and offers the two primary actions:
 * "Start chat" (creates a session bound to this assistant → navigates to chat)
 * and "Edit" (opens the builder). Purely presentational: the parent owns the
 * mutations and passes the in-flight flag for THIS card so only it shows a busy
 * "Starting…" label.
 *
 * A11y: a labelled <article>; the card title is a heading; every action is a real
 * keyboard-reachable button. Long names/emails truncate rather than break layout.
 */
import { Link } from 'react-router-dom';
import { Card } from '@/components/Card';
import { StatusBadge } from '@/components/StatusBadge';
import { Icon } from '@/ui';
import type { Assistant, ChatModelInfo, Member } from '@/api';
import {
  effectiveAutonomyDisplay,
  certificationBadge,
  modelLabel,
  ownerLabel,
  STATUS_LABEL,
  statusTone,
} from '../model/presentation';

/** Cap tool badges so a long allow-list never blows out the card. */
const MAX_TOOL_BADGES = 4;

interface AssistantCardProps {
  assistant: Assistant;
  models: ChatModelInfo[] | undefined;
  members: Member[] | undefined;
  /** True while THIS card's "Start chat" mutation is in flight. */
  starting: boolean;
  /** Disable "Start chat" (e.g. a disabled assistant can't start a session). */
  startDisabled?: boolean;
  onStart: (assistant: Assistant) => void;
  /** A per-card start error message (e.g. a 422/404 from create-session). */
  startError?: string | null;
}

export function AssistantCard({
  assistant,
  models,
  members,
  starting,
  startDisabled = false,
  onStart,
  startError,
}: AssistantCardProps) {
  const owner = ownerLabel(assistant.owner, members);
  const model = modelLabel(assistant.model, models);
  const autonomy = effectiveAutonomyDisplay(assistant);
  const tools = assistant.toolAllowlist;
  const shownTools = tools.slice(0, MAX_TOOL_BADGES);
  const overflow = tools.length - shownTools.length;
  const certification = certificationBadge(assistant.certificationState);

  return (
    <Card
      role="article"
      aria-label={`Assistant ${assistant.name}`}
      className="flex h-full min-w-0 flex-col p-4"
    >
      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span
            aria-hidden="true"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-surface-muted text-foreground-muted"
          >
            <Icon name="sparkles" />
          </span>
          <h3 className="truncate text-sm font-semibold" title={assistant.name}>
            {assistant.name}
          </h3>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          {assistant.featured ? (
            <span
              className="inline-flex items-center gap-1 rounded-full bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent"
              title="Featured in the library by an admin"
            >
              <Icon name="sparkles" className="shrink-0" aria-hidden="true" />
              Featured
            </span>
          ) : null}
          {certification ? (
            <StatusBadge tone={certification.tone}>{certification.label}</StatusBadge>
          ) : null}
          <StatusBadge tone={statusTone(assistant.status)}>
            {STATUS_LABEL[assistant.status]}
          </StatusBadge>
        </div>
      </div>

      {assistant.description ? (
        <p className="mt-2 line-clamp-2 text-sm text-foreground-muted" title={assistant.description}>
          {assistant.description}
        </p>
      ) : (
        <p className="mt-2 text-sm italic text-foreground-muted">No description</p>
      )}

      <dl className="mt-3 space-y-1 text-xs text-foreground-muted">
        <div className="flex min-w-0 items-center gap-1.5">
          <Icon name="user" className="shrink-0" />
          <dt className="sr-only">Owner</dt>
          <dd className="truncate" title={owner}>
            {owner}
          </dd>
        </div>
        <div className="flex min-w-0 items-center gap-1.5">
          <Icon name="sliders" className="shrink-0" />
          <dt className="sr-only">Model</dt>
          <dd className="truncate" title={model}>
            {model}
          </dd>
        </div>
        <div className="flex min-w-0 items-center gap-1.5">
          <Icon name="shield-check" className="shrink-0" />
          <dt className="sr-only">Autonomy</dt>
          <dd
            className="truncate"
            title={
              autonomy.capped
                ? `Autonomy ${autonomy.label} (capped by the tenant admin ceiling)`
                : `Autonomy ${autonomy.label}`
            }
          >
            {autonomy.label}
            {autonomy.capped ? (
              <span className="ml-1 rounded-full bg-warn/15 px-1.5 py-0.5 text-[10px] font-medium text-warn">
                capped
              </span>
            ) : null}
          </dd>
        </div>
      </dl>

      <div className="mt-3 flex flex-wrap gap-1.5" aria-label="Allowed tools">
        {tools.length === 0 ? (
          <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-foreground-muted">
            Default tools
          </span>
        ) : (
          <>
            {shownTools.map((tool) => (
              <span
                key={tool}
                className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent"
              >
                {tool}
              </span>
            ))}
            {overflow > 0 ? (
              <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-foreground-muted">
                +{overflow}
              </span>
            ) : null}
          </>
        )}
      </div>

      <div className="mt-auto flex items-center gap-2 pt-4">
        <button
          type="button"
          onClick={() => onStart(assistant)}
          disabled={starting || startDisabled}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name="message-square" className="shrink-0" />
          {starting ? 'Starting…' : 'Start chat'}
        </button>
        <Link
          to={`/assistants/${assistant.id}`}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Icon name="pencil" className="shrink-0" />
          Edit
        </Link>
      </div>

      {startError ? (
        <p role="alert" className="mt-2 flex items-start gap-1.5 text-xs text-danger">
          <Icon name="alert-triangle" className="mt-px shrink-0" />
          <span>{startError}</span>
        </p>
      ) : null}
    </Card>
  );
}
