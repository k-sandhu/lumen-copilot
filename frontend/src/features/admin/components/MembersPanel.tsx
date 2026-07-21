/**
 * MembersPanel — the Members & roles roster (#88/#122, ADR-0007 §4; #455,
 * ADR-0019 §2). Lists the tenant's members, the RBAC roles each holds (member /
 * admin / security, spec 0004 §2.3), and — since managed connectors landed —
 * each member's IDENTITY ATTESTATION state with the one mutating control this
 * panel carries: **Attest identity** (`POST /admin/members/{id}/attest-identity`,
 * audited server-side as `user.identity_attested`). Connector ACL mirroring
 * maps a source permission to a Lumen user ONLY when that user's email is
 * attested — unattested members never receive mirrored access, and a source's
 * `unmapped_acl_count` is exactly what attestation lights up.
 *
 * There is still no invite / role edit / remove — those stay read-only for v1.
 * The roster is tenant-scoped (INV-1) and admin-only; a non-admin (403, INV-5)
 * or expired session (401, INV-4) renders as an actionable error via PanelBody,
 * and a failed attest renders INLINE on its row — never a blank pane.
 *
 * The avatar initials are derived client-side from the email purely for
 * legibility — honest derivation, not invented data.
 */
import { useState } from 'react';
import { StatusDot, type StatusTone } from '@/ui';
import { ApiError } from '@/api';
import type { Member, UserRole } from '@/api';
import { useAttestMemberIdentity, useMembers } from '../model/queries';
import { PanelBody } from './PanelState';

/** Two-letter avatar initials derived from the email local part (e.g.
 *  "avery.madison@…" → "AM"). Purely presentational; no data is invented. */
function initialsFromEmail(email: string): string {
  const local = email.split('@')[0] ?? email;
  const parts = local.split(/[.\-_+]/).filter(Boolean);
  const first = parts[0]?.[0];
  const second = parts[1]?.[0];
  const letters = first && second ? `${first}${second}` : local.slice(0, 2) || '?';
  return letters.toUpperCase();
}

/** Map a role to a status tone so the table reads at a glance. */
const ROLE_TONE: Record<UserRole, StatusTone> = {
  admin: 'warn',
  security: 'danger',
  member: 'muted',
};

const ROLE_LABEL: Record<UserRole, string> = {
  admin: 'Admin',
  security: 'Security',
  member: 'Member',
};

/** Best inline message for a failed attest action. */
function attestErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'You need the admin role to attest identities.';
    if (error.status === 404) return 'That member no longer exists.';
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    return error.displayMessage;
  }
  return "Couldn't attest this member. Please try again.";
}

/** Short date for the attested-at stamp, e.g. "Jul 18, 2026". */
function attestedDate(iso: string): string {
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return iso;
  return new Date(ts).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

function RoleBadges({ roles }: { roles: UserRole[] }) {
  if (roles.length === 0) {
    return <span className="text-sm text-foreground-muted">—</span>;
  }
  return (
    <span className="flex flex-wrap gap-2">
      {roles.map((role) => (
        <StatusDot key={role} tone={ROLE_TONE[role]} label={ROLE_LABEL[role]} />
      ))}
    </span>
  );
}

function MemberCell({ member }: { member: Member }) {
  return (
    <div className="flex items-center gap-3">
      <span
        aria-hidden="true"
        className="flex h-8 w-8 flex-none items-center justify-center rounded-full bg-surface-muted text-xs font-semibold text-foreground-muted"
      >
        {initialsFromEmail(member.email)}
      </span>
      <span className="font-medium text-foreground">{member.email}</span>
    </div>
  );
}

/** The attestation state + the admin attest action for one member row. */
function AttestationCell({
  member,
  busy,
  error,
  onAttest,
}: {
  member: Member;
  busy: boolean;
  error: unknown | null;
  onAttest: (member: Member) => void;
}) {
  const attested = member.email_attested_at !== null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {member.email_attested_at !== null ? (
        <StatusDot tone="ok" label={`Attested ${attestedDate(member.email_attested_at)}`} />
      ) : (
        <StatusDot tone="muted" label="Unattested" />
      )}
      <button
        type="button"
        onClick={() => onAttest(member)}
        disabled={busy}
        aria-label={`${attested ? 'Re-attest' : 'Attest'} identity for ${member.email}`}
        className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
      >
        {busy ? 'Attesting…' : attested ? 'Re-attest' : 'Attest identity'}
      </button>
      {error !== null ? (
        <p role="alert" className="basis-full text-xs text-danger">
          {attestErrorMessage(error)}
        </p>
      ) : null}
    </div>
  );
}

function MembersTable({
  members,
  attestingId,
  attestError,
  onAttest,
}: {
  members: Member[];
  attestingId: string | null;
  attestError: { id: string; error: unknown } | null;
  onAttest: (member: Member) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">
          Tenant members, their roles, and their identity-attestation state
        </caption>
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-foreground-muted">
            <th scope="col" className="px-4 py-2 font-medium">
              Member
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Roles
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Identity attestation
            </th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => (
            <tr key={member.id} className="border-b border-border/60 last:border-0">
              <td className="px-4 py-3 align-middle">
                <MemberCell member={member} />
              </td>
              <td className="px-4 py-3 align-middle">
                <RoleBadges roles={member.role} />
              </td>
              <td className="px-4 py-3 align-middle">
                <AttestationCell
                  member={member}
                  busy={attestingId === member.id}
                  error={attestError?.id === member.id ? attestError.error : null}
                  onAttest={onAttest}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MembersPanel() {
  const query = useMembers();
  const attest = useAttestMemberIdentity();
  const members = query.data?.items ?? [];

  // Track which row the attest mutation targets so only it shows busy/error.
  const [attestingId, setAttestingId] = useState<string | null>(null);
  const [attestError, setAttestError] = useState<{ id: string; error: unknown } | null>(null);

  const handleAttest = (member: Member) => {
    setAttestingId(member.id);
    setAttestError(null);
    attest.mutate(member.id, {
      onError: (error) => setAttestError({ id: member.id, error }),
      onSuccess: () => setAttestError(null),
      onSettled: () => setAttestingId(null),
    });
  };

  return (
    <section aria-labelledby="admin-members-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-members-heading" className="text-sm font-semibold text-foreground">
          Members &amp; roles
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          This tenant&rsquo;s members and their RBAC roles. Attesting a member&rsquo;s identity lets
          connector permission mirroring map source access to them (audited) — unattested members
          never receive mirrored connector access.
        </p>
      </header>
      <PanelBody
        label="members"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={members.length === 0}
        emptyMessage="No members in this tenant yet."
        onRetry={() => void query.refetch()}
        loadingRows={4}
      >
        <MembersTable
          members={members}
          attestingId={attestingId}
          attestError={attestError}
          onAttest={handleAttest}
        />
      </PanelBody>
    </section>
  );
}
