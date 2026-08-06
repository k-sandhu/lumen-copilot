/**
 * GroupMembersSection — the roster of one user group, and the two writes that
 * change who a share reaches (#540, ADR-0022). Opened from a row in
 * GroupsPanel; never rendered for the derived "All members" group, whose
 * membership is not enumerable (ADR-0022 §3). GroupsPanel keys it by group id,
 * so selecting a different group remounts it rather than carrying this one's
 * picker choice and inline errors across.
 *
 * The picker walks the WHOLE tenant roster by cursor, not just its first page —
 * a picker capped at 20 could not add a tenant's 21st member — and it offers
 * only people not already in the group. It claims "everyone is already in this
 * group" only once the last page has actually been read; while pages remain it
 * says so and offers to load them.
 *
 * Removing a member takes effect on that person's NEXT request — membership is
 * re-read per request and never cached in their token (ADR-0022 §7) — and the
 * copy says so, because "when does this actually revoke?" is the question an
 * admin is really asking.
 *
 * States: loading, empty ("No one in this group yet"), error with Retry, and a
 * dead end with no Retry for 401/403 (not allowed) and 404 (the group was
 * deleted under us — refetching a dead id only 404s again). Write controls are
 * withheld in those same states rather than inviting a guaranteed refusal.
 * Each removal owns its own pending flag and its own error, so two overlapping
 * removals cannot borrow each other's outcome.
 */
import { useRef, useState } from 'react';
import type { Group, Member } from '@/api';
import {
  useAddGroupMember,
  useGroupMembers,
  useMemberRoster,
  useRemoveGroupMember,
} from '../model/queries';
import { GROUP_READ_DEAD_ENDS, describeMemberWriteError, isDeadEnd, isGone } from './groupErrors';
import { PanelBody } from './PanelState';

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
  const roster = useMemberRoster();
  const add = useAddGroupMember();
  const remove = useRemoveGroupMember();

  const members = query.data?.items ?? [];
  const [choice, setChoice] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  // Per-member so two overlapping removals never share a spinner or an error.
  const [removingIds, setRemovingIds] = useState<ReadonlySet<string>>(() => new Set());
  const [removeErrors, setRemoveErrors] = useState<Readonly<Record<string, string>>>({});
  // A permanently mounted live region: a polite region created at the same time
  // as its text is often missed by screen readers, and both writes destroy the
  // control that had focus, so the outcome has to be spoken.
  const [announcement, setAnnouncement] = useState('');
  const pickerRef = useRef<HTMLSelectElement>(null);

  const memberIds = new Set(members.map((m) => m.id));
  const rosterMembers = (roster.data?.pages ?? []).flatMap((page) => page.items);
  const candidates = rosterMembers.filter((m) => !memberIds.has(m.id));
  const morePages = roster.hasNextPage === true;

  // Neither a refusal nor a deleted group can be fixed by trying again, so the
  // write controls go away with the retry button.
  const writesBlocked = isDeadEnd(query.error) || isGone(query.error);

  const setRemoving = (id: string, busy: boolean) =>
    setRemovingIds((prev) => {
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });

  const clearRemoveError = (id: string) =>
    setRemoveErrors((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });

  const handleAdd = async (event: React.FormEvent) => {
    event.preventDefault();
    if (choice === '') return;
    // Resolve the label before the write, because a successful add removes the
    // person from the candidate list.
    const added = candidates.find((m) => m.id === choice);
    setAddError(null);
    try {
      await add.mutateAsync({ groupId: group.id, userId: choice });
      setChoice('');
      setAnnouncement(`Added ${added?.email ?? 'that member'} to ${group.name}.`);
    } catch (error) {
      setAddError(describeMemberWriteError(error));
    }
  };

  const handleRemove = async (member: Member) => {
    clearRemoveError(member.id);
    setRemoving(member.id, true);
    try {
      await remove.mutateAsync({ groupId: group.id, userId: member.id });
      setAnnouncement(`Removed ${member.email} from ${group.name}.`);
      // The row — and the button holding focus — is about to unmount. Land
      // focus on the picker rather than letting it fall to <body>.
      pickerRef.current?.focus();
    } catch (error) {
      setRemoveErrors((prev) => ({ ...prev, [member.id]: describeMemberWriteError(error) }));
    } finally {
      setRemoving(member.id, false);
    }
  };

  return (
    <section
      aria-labelledby={`group-members-${group.id}`}
      className="border-t border-border bg-surface-muted/20"
    >
      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>
      <header className="px-4 py-3">
        <h3 id={`group-members-${group.id}`} className="text-sm font-semibold text-foreground">
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
        deadEndStatuses={GROUP_READ_DEAD_ENDS}
        loadingRows={2}
      >
        <ul aria-label={`Members of ${group.name}`} className="border-t border-border">
          {members.map((member) => (
            <MemberRow
              key={member.id}
              member={member}
              busy={removingIds.has(member.id)}
              error={removeErrors[member.id] ?? null}
              onRemove={() => void handleRemove(member)}
            />
          ))}
        </ul>
      </PanelBody>

      {writesBlocked ? null : (
        <form
          aria-label={`Add a member to ${group.name}`}
          onSubmit={(event) => void handleAdd(event)}
          className="flex flex-wrap items-end gap-3 border-t border-border px-4 py-3"
        >
          <label
            htmlFor={`add-member-${group.id}`}
            className="flex flex-col gap-1 text-xs font-medium text-foreground-muted"
          >
            Add member
            <select
              id={`add-member-${group.id}`}
              ref={pickerRef}
              value={choice}
              disabled={roster.isLoading || candidates.length === 0}
              aria-busy={roster.isLoading}
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

          {morePages ? (
            <button
              type="button"
              onClick={() => void roster.fetchNextPage()}
              disabled={roster.isFetchingNextPage}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
            >
              {roster.isFetchingNextPage ? 'Loading…' : 'Load more members'}
            </button>
          ) : null}

          {roster.error !== null ? (
            <p role="alert" className="basis-full text-xs text-danger">
              {isDeadEnd(roster.error)
                ? 'You need the admin role to list this tenant’s members, so there is no one to choose from.'
                : 'Couldn’t load the member roster, so there is no one to choose from.'}{' '}
              {/* Same rule as the panel body: a refusal is a dead end, not a retry. */}
              {isDeadEnd(roster.error) ? null : (
                <button
                  type="button"
                  onClick={() => void roster.refetch()}
                  className="underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  Retry
                </button>
              )}
            </p>
          ) : roster.isLoading ? (
            <p className="basis-full text-xs text-foreground-muted" role="status">
              Loading this tenant&rsquo;s members…
            </p>
          ) : morePages ? (
            <p className="basis-full text-xs text-foreground-muted" role="note">
              More members are available — load them to see everyone.
            </p>
          ) : !roster.isLoading && candidates.length === 0 ? (
            // Only truthful once the cursor is exhausted; with pages left this
            // would claim the tenant is fully enrolled when it may not be.
            <p className="basis-full text-xs text-foreground-muted" role="note">
              Everyone in this tenant is already in this group.
            </p>
          ) : null}

          {addError !== null ? (
            <p role="alert" className="basis-full text-xs text-danger">
              {addError}
            </p>
          ) : null}
        </form>
      )}
    </section>
  );
}
