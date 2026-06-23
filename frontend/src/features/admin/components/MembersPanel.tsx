/**
 * MembersPanel — the read-only Members & roles roster (#88, ADR-0007 §4). Lists
 * the tenant's members and the RBAC roles each holds (member / admin / security,
 * spec 0004 §2.3). There are NO mutating controls: no invite, no role edit, no
 * remove — admin is read-mostly for v1. The roster is tenant-scoped (INV-1) and
 * admin-only; a non-admin (403, INV-5) or expired session (401, INV-4) renders as
 * an actionable error via PanelBody.
 */
import { StatusDot, type StatusTone } from '@/ui';
import type { Member, UserRole } from '@/api';
import { useMembers } from '../model/queries';
import { PanelBody } from './PanelState';

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

function MembersTable({ members }: { members: Member[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Tenant members and their roles</caption>
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-foreground-muted">
            <th scope="col" className="px-4 py-2 font-medium">
              Member
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Roles
            </th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => (
            <tr key={member.id} className="border-b border-border/60 last:border-0">
              <td className="px-4 py-3 align-top">
                <span className="font-medium text-foreground">{member.email}</span>
              </td>
              <td className="px-4 py-3 align-top">
                <RoleBadges roles={member.role} />
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
  const members = query.data?.items ?? [];

  return (
    <section aria-labelledby="admin-members-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-members-heading" className="text-sm font-semibold text-foreground">
          Members &amp; roles
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Read-only roster of this tenant&rsquo;s members and their RBAC roles.
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
        <MembersTable members={members} />
      </PanelBody>
    </section>
  );
}
