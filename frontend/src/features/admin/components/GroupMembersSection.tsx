/**
 * GroupMembersSection — the roster of one user group, and the two writes that
 * change who a share reaches (#540, ADR-0022). Opened from a row in
 * GroupsPanel; never rendered for the derived "All members" group, whose
 * membership is not enumerable (ADR-0022 §3).
 *
 * The picker offers the tenant roster minus whoever is already in the group, so
 * the only offered action is one that changes something. Removing a member
 * takes effect on that person's NEXT request — membership is re-read per
 * request and never cached in their token (ADR-0022 §7) — and the copy says so,
 * because "when does this actually revoke?" is the question an admin is really
 * asking.
 *
 * States: loading, empty ("No one in this group yet"), error with Retry, the
 * role-gated 403/401 dead end, and inline write failures. The roster read that
 * feeds the picker has its own states — if it fails, the picker says so instead
 * of silently offering an empty list that looks like "everyone is already in".
 */
import { useState } from 'react';
import type { Group, Member } from '@/api';
import {
  useAddGroupMember,
  useGroupMembers,
  useMembers,
  useRemoveGroupMember,
} from '../model/queries';
import { describeMemberWriteError } from './groupErrors';
import { PanelBody } from './PanelState';

/** A role-gated read: neither 403 nor 401 is fixable by fetching again. */
const READ_DEAD_ENDS = [401, 403];

function MemberRow({
  member,
  busy,
  error,
  onRemove,
}: {
  member: Member;
  busy: boolean;
  error: string | null;
  onRemove: () => void;
}) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-4 py-2 last:border-0">
      <span className="text-sm text-foreground">{member.email}</span>
      <button
        type="button"
        onClick={onRemove}
        disabled={busy}
        aria-label={`Remove ${member.email} from the group`}
        className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
      >
        {busy ? 'Removing…' : 'Remove'}
      </button>
      {error !== null ? (
        <p role="alert" className="basis-full text-xs text-danger">
          {error}
        </p>
      ) : null}
    </li>
  );
}

export function GroupMembersSection({ group }: { group: Group }) {
  const query = useGroupMembers(group.id);
  const roster = useMembers();
  const add = useAddGroupMember();
  const remove = useRemoveGroupMember();

  const members = query.data?.items ?? [];
  const [choice, setChoice] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [removeError, setRemoveError] = useState<{ id: string; message: string } | null>(null);

  const memberIds = new Set(members.map((m) => m.id));
  const candidates = (roster.data?.items ?? []).filter((m) => !memberIds.has(m.id));
  // The roster is a cursor page; with more than one page the picker cannot
  // offer everyone, and saying so beats a silently short list.
  const rosterTruncated = Boolean(roster.data?.next_cursor);

  const removingId = remove.isPending ? (remove.variables?.userId ?? null) : null;

  const handleAdd = (event: React.FormEvent) => {
    event.preventDefault();
    if (choice === '') return;
    setAddError(null);
    add.mutate(
      { groupId: group.id, userId: choice },
      {
        onSuccess: () => setChoice(''),
        onError: (error) => setAddError(describeMemberWriteError(error)),
      },
    );
  };

  const handleRemove = (member: Member) => {
    setRemoveError(null);
    remove.mutate(
      { groupId: group.id, userId: member.id },
      {
        onError: (error) =>
          setRemoveError({ id: member.id, message: describeMemberWriteError(error) }),
      },
    );
  };

  return (
    <section
      aria-labelledby={`group-members-${group.id}`}
      className="border-t border-border bg-surface-muted/20"
    >
      <header className="px-4 py-3">
        <h3
          id={`group-members-${group.id}`}
          className="text-sm font-semibold text-foreground"
        >
          Members of {group.name}
        </h3>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Adding someone grants them everything shared with this group. Removing someone revokes it
          on their next request.
        </p>
      </header>

      <PanelBody
        label={`members of ${group.name}`}
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={members.length === 0}
        emptyMessage="No one in this group yet. It reaches nobody until you add someone."
        onRetry={() => void query.refetch()}
        deadEndStatuses={READ_DEAD_ENDS}
        loadingRows={2}
      >
        <ul aria-label={`Members of ${group.name}`} className="border-t border-border">
          {members.map((member) => (
            <MemberRow
              key={member.id}
              member={member}
              busy={removingId === member.id}
              error={removeError?.id === member.id ? removeError.message : null}
              onRemove={() => handleRemove(member)}
            />
          ))}
        </ul>
      </PanelBody>

      <form
        aria-label={`Add a member to ${group.name}`}
        onSubmit={handleAdd}
        className="flex flex-wrap items-end gap-3 border-t border-border px-4 py-3"
      >
        <label
          htmlFor={`add-member-${group.id}`}
          className="flex flex-col gap-1 text-xs font-medium text-foreground-muted"
        >
          Add member
          <select
            id={`add-member-${group.id}`}
            value={choice}
            disabled={roster.isLoading || candidates.length === 0}
            onChange={(event) => setChoice(event.target.value)}
            className="w-64 rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            <option value="">Choose a member…</option>
            {candidates.map((member) => (
              <option key={member.id} value={member.id}>
                {member.email}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          disabled={choice === '' || add.isPending}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          {add.isPending ? 'Adding…' : 'Add'}
        </button>

        {roster.error !== null ? (
          <p role="alert" className="basis-full text-xs text-danger">
            Couldn&rsquo;t load the member roster, so there is no one to choose from.{' '}
            <button
              type="button"
              onClick={() => void roster.refetch()}
              className="underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Retry
            </button>
          </p>
        ) : !roster.isLoading && candidates.length === 0 ? (
          <p className="basis-full text-xs text-foreground-muted" role="note">
            Everyone in this tenant is already in this group.
          </p>
        ) : null}

        {rosterTruncated ? (
          <p className="basis-full text-xs text-foreground-muted" role="note">
            Only the first page of the member roster is listed here.
          </p>
        ) : null}

        {addError !== null ? (
          <p role="alert" className="basis-full text-xs text-danger">
            {addError}
          </p>
        ) : null}
      </form>
    </section>
  );
}
