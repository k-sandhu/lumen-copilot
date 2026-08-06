/**
 * GroupsPanel — the admin's group management surface (#540, ADR-0022). A group
 * is the tenant-scoped membership primitive that a source grant can name:
 * sharing with "Tax Team" reaches whoever is in Tax Team *at request time*, so
 * adding and removing people here is how access is widened and revoked.
 *
 * Two kinds of row, and the difference is load-bearing:
 *   - `user` groups an admin creates, renames, deletes and populates.
 *   - the single `system` group ("All members"), which expresses tenant-wide
 *     visibility. Its membership is DERIVED, not stored, so it has no roster to
 *     edit and no count to show — `member_count: null` means *everyone*, not
 *     zero, and this panel says "Everyone in this tenant" rather than "0".
 *     It renders read-only: no rename, no delete, no member controls (ADR-0022
 *     §3). The server enforces the same thing with 409 `system_group_immutable`.
 *
 * States (frontend/AGENTS.md "every state, not just success"): loading skeleton,
 * empty ("No groups yet"), read error with Retry, and — because this read is
 * role-gated — a 403 (INV-5) / 401 (INV-4) rendered as an honest dead end with
 * NO retry button AND no create form, since neither could succeed. Write
 * failures render inline (a dismissible toast, or inside the confirm dialog)
 * and are never discarded — including the typed name, which survives a rejected
 * create so the admin can correct it rather than retype it.
 *
 * Concurrency is assumed, not hoped for: another admin can delete a group
 * between this render and a click. Every write that comes back 404 refreshes
 * the list (in the hook) so the stale row disappears, and a delete that 404s is
 * treated as the outcome the admin wanted — the group is gone.
 *
 * Tenant-scoped throughout (INV-1): an unknown or cross-tenant group is 404,
 * never 403.
 */
import { useEffect, useRef, useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { StatusDot } from '@/ui';
import type { Group } from '@/api';
import { useCreateGroup, useDeleteGroup, useGroups, useRenameGroup } from '../model/queries';
import { GroupMembersSection } from './GroupMembersSection';
import { READ_DEAD_ENDS, describeGroupWriteError, isDeadEnd, isGone } from './groupErrors';
import { PanelBody } from './PanelState';

const MAX_NAME_LENGTH = 120;

interface Toast {
  kind: 'ok' | 'error';
  message: string;
}

/**
 * How many people this group reaches. The system group's `null` count is the
 * whole tenant — rendering it as a number would state the opposite of the truth.
 */
function MemberCount({ group }: { group: Group }) {
  if (group.member_count === null) {
    return <StatusDot tone="ok" label="Everyone in this tenant" />;
  }
  const n = group.member_count;
  return <span className="text-foreground-muted">{n === 1 ? '1 member' : `${n} members`}</span>;
}

/** Inline rename form for one row — replaces the name cell while editing. */
function RenameForm({
  group,
  busy,
  onSubmit,
  onCancel,
}: {
  group: Group;
  busy: boolean;
  onSubmit: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(group.name);
  const trimmed = name.trim();
  const canSubmit = trimmed.length > 0 && trimmed.length <= MAX_NAME_LENGTH && !busy;

  return (
    <form
      aria-label={`Rename ${group.name}`}
      className="flex flex-wrap items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSubmit) onSubmit(trimmed);
      }}
    >
      <label className="sr-only" htmlFor={`rename-${group.id}`}>
        New name for {group.name}
      </label>
      <input
        id={`rename-${group.id}`}
        value={name}
        autoFocus
        maxLength={MAX_NAME_LENGTH}
        onChange={(event) => setName(event.target.value)}
        className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      />
      <button
        type="submit"
        disabled={!canSubmit}
        className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
      >
        {busy ? 'Saving…' : 'Save'}
      </button>
      <button
        type="button"
        onClick={onCancel}
        disabled={busy}
        className="rounded-md px-2 py-1 text-xs text-foreground-muted hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
      >
        Cancel
      </button>
    </form>
  );
}

function GroupRow({
  group,
  editing,
  busy,
  selected,
  onRename,
  onStartRename,
  onCancelRename,
  onDelete,
  onToggleMembers,
}: {
  group: Group;
  editing: boolean;
  busy: boolean;
  selected: boolean;
  onRename: (name: string) => void;
  onStartRename: () => void;
  onCancelRename: () => void;
  onDelete: () => void;
  onToggleMembers: () => void;
}) {
  const isSystem = group.kind === 'system';
  // `autoFocus` moves focus INTO the rename form; nothing moved it back out, so
  // saving or cancelling dropped focus to <body>. Return it to the control that
  // opened the form (frontend/AGENTS.md: managed focus on async updates).
  const renameRef = useRef<HTMLButtonElement>(null);
  const wasEditing = useRef(editing);
  useEffect(() => {
    if (wasEditing.current && !editing) renameRef.current?.focus();
    wasEditing.current = editing;
  }, [editing]);

  const action =
    'rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50';

  return (
    <tr className="border-b border-border/60 last:border-0">
      <td className="px-4 py-3 align-middle">
        {editing ? (
          <RenameForm group={group} busy={busy} onSubmit={onRename} onCancel={onCancelRename} />
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-foreground">{group.name}</span>
            {isSystem ? (
              <span className="rounded-full border border-border px-2 py-0.5 text-xs text-foreground-muted">
                Managed automatically
              </span>
            ) : null}
          </div>
        )}
      </td>
      <td className="px-4 py-3 align-middle">
        <MemberCount group={group} />
      </td>
      <td className="px-4 py-3 align-middle">
        {isSystem ? (
          // Nothing to manage: membership is derived, so there is no roster to
          // edit and no name to change (ADR-0022 §3).
          <span className="text-xs text-foreground-muted">Every member of this tenant, always</span>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onToggleMembers}
              aria-expanded={selected}
              aria-label={`${selected ? 'Hide' : 'Manage'} members of ${group.name}`}
              className={action}
            >
              {selected ? 'Hide members' : 'Manage members'}
            </button>
            <button
              ref={renameRef}
              type="button"
              onClick={onStartRename}
              disabled={editing || busy}
              aria-label={`Rename ${group.name}`}
              className={action}
            >
              Rename
            </button>
            <button
              type="button"
              onClick={onDelete}
              disabled={busy}
              aria-label={`Delete ${group.name}`}
              className={`${action} text-danger`}
            >
              Delete
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}

function CreateGroupForm({
  busy,
  onCreate,
}: {
  busy: boolean;
  /** Resolves true when the group was created, so a rejected name survives. */
  onCreate: (name: string) => Promise<boolean>;
}) {
  const [name, setName] = useState('');
  const trimmed = name.trim();
  const canSubmit = trimmed.length > 0 && trimmed.length <= MAX_NAME_LENGTH && !busy;

  return (
    <form
      aria-label="Create group"
      className="flex flex-wrap items-end gap-3 border-t border-border bg-surface-muted/40 px-4 py-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!canSubmit) return;
        // Clear only once the server has accepted it — a 409 on a duplicate
        // name must leave the text there to correct, not force a retype.
        void onCreate(trimmed).then((created) => {
          if (created) setName('');
        });
      }}
    >
      <label
        htmlFor="new-group-name"
        className="flex flex-col gap-1 text-xs font-medium text-foreground-muted"
      >
        Group name
        <input
          id="new-group-name"
          value={name}
          maxLength={MAX_NAME_LENGTH}
          placeholder="Tax Team"
          onChange={(event) => setName(event.target.value)}
          className="w-64 rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </label>
      <button
        type="submit"
        disabled={!canSubmit}
        className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
      >
        {busy ? 'Creating…' : 'Create group'}
      </button>
    </form>
  );
}

export function GroupsPanel() {
  const query = useGroups();
  const create = useCreateGroup();
  const rename = useRenameGroup();
  const remove = useDeleteGroup();

  const groups = query.data?.items ?? [];

  const [toast, setToast] = useState<Toast | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Group | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // Which rows have a write in flight. A Set rather than one id because two
  // rows can be mid-flight at once, and each call must own its own outcome.
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(() => new Set());

  // A group can vanish under us (another admin deleted it), so resolve the
  // selection against the live list rather than trusting the stored id.
  const selected = groups.find((g) => g.id === selectedId) ?? null;

  // A read the caller is not allowed to make: creating would be refused too, so
  // the form is not offered. The 403 on a write stays a backstop for a race.
  const readForbidden = isDeadEnd(query.error);

  const markBusy = (id: string, busy: boolean) =>
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });

  const handleCreate = async (name: string): Promise<boolean> => {
    setToast(null);
    try {
      const group = await create.mutateAsync({ name });
      setToast({ kind: 'ok', message: `Created ${group.name}.` });
      return true;
    } catch (error) {
      setToast({ kind: 'error', message: describeGroupWriteError(error) });
      return false;
    }
  };

  const handleRename = async (group: Group, name: string) => {
    setToast(null);
    markBusy(group.id, true);
    try {
      const updated = await rename.mutateAsync({ groupId: group.id, name });
      // Only close the form belonging to THIS group — a slower rename must not
      // discard a draft the admin has since opened on another row.
      setEditingId((current) => (current === group.id ? null : current));
      setToast({ kind: 'ok', message: `Renamed to ${updated.name}.` });
    } catch (error) {
      setToast({ kind: 'error', message: describeGroupWriteError(error) });
    } finally {
      markBusy(group.id, false);
    }
  };

  const handleDelete = async () => {
    if (pendingDelete === null) return;
    const group = pendingDelete;
    setDeleteError(null);
    markBusy(group.id, true);
    const finish = (message: string) => {
      setPendingDelete(null);
      setSelectedId((current) => (current === group.id ? null : current));
      setEditingId((current) => (current === group.id ? null : current));
      setToast({ kind: 'ok', message });
    };
    try {
      await remove.mutateAsync(group.id);
      finish(
        `Deleted ${group.name}. Anything shared with it is no longer reachable through it.`,
      );
    } catch (error) {
      // A 404 means someone else already deleted it — that IS the outcome the
      // admin asked for, so report it as done rather than as a failure they
      // must act on. The hook has already refreshed the list.
      if (isGone(error)) {
        finish(`${group.name} was already deleted.`);
      } else {
        // Keep the dialog open so the failure lands where the action was taken.
        setDeleteError(describeGroupWriteError(error));
      }
    } finally {
      markBusy(group.id, false);
    }
  };

  return (
    <section aria-labelledby="admin-groups-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-groups-heading" className="text-sm font-semibold text-foreground">
          Groups
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Groups are how a source is shared with a set of people. A grant to a group applies to
          whoever is in it at the time of the request, so adding someone here grants access
          immediately and removing them revokes it on their next request. Membership alone confers
          nothing — roles still decide what a person may do.
        </p>
      </header>

      {toast !== null ? (
        <div
          role={toast.kind === 'error' ? 'alert' : 'status'}
          className={`flex items-start justify-between gap-3 border-b border-border px-4 py-2 text-sm ${
            toast.kind === 'error' ? 'bg-danger/10 text-danger' : 'bg-surface-muted text-foreground'
          }`}
        >
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => setToast(null)}
            className="rounded-md px-2 py-0.5 text-xs hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Dismiss
          </button>
        </div>
      ) : null}

      <PanelBody
        label="groups"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={groups.length === 0}
        emptyMessage="No groups yet. Create one below to share sources with a set of people."
        onRetry={() => void query.refetch()}
        deadEndStatuses={READ_DEAD_ENDS}
        loadingRows={3}
      >
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-sm">
            <caption className="sr-only">
              This tenant&rsquo;s groups, how many people each reaches, and the controls to manage
              them
            </caption>
            <thead>
              <tr className="border-b border-border text-xs uppercase tracking-wide text-foreground-muted">
                <th scope="col" className="px-4 py-2 font-medium">
                  Group
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Reaches
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <GroupRow
                  key={group.id}
                  group={group}
                  editing={editingId === group.id}
                  busy={busyIds.has(group.id)}
                  selected={selectedId === group.id}
                  onRename={(name) => void handleRename(group, name)}
                  onStartRename={() => {
                    setToast(null);
                    setEditingId(group.id);
                  }}
                  onCancelRename={() => setEditingId(null)}
                  onDelete={() => {
                    setToast(null);
                    setDeleteError(null);
                    setPendingDelete(group);
                  }}
                  onToggleMembers={() =>
                    setSelectedId((current) => (current === group.id ? null : group.id))
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      </PanelBody>

      {/*
        Outside PanelBody on purpose: an admin needs the create form in exactly
        the states PanelBody replaces — the empty list most of all. But not when
        the read was REFUSED: offering a write that can only be refused too is
        the same dead end the retry button would have been.
      */}
      {readForbidden ? null : (
        <CreateGroupForm busy={create.isPending} onCreate={handleCreate} />
      )}

      {/*
        Keyed by group id so switching selection REMOUNTS the section. Without
        it the previous group's picker choice and inline errors would carry over
        and could be submitted against the newly selected group.
      */}
      {selected !== null ? <GroupMembersSection key={selected.id} group={selected} /> : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`Delete ${pendingDelete?.name ?? 'group'}?`}
        description="This removes the group, its membership, and every share that names it. People who reached those sources only through this group will lose access."
        confirmLabel="Delete group"
        busyLabel="Deleting…"
        busy={remove.isPending}
        error={deleteError}
        onConfirm={() => void handleDelete()}
        onCancel={() => {
          setPendingDelete(null);
          setDeleteError(null);
        }}
      />
    </section>
  );
}
