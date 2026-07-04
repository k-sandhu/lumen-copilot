/**
 * Brand cell (issue #110) — the top-left identity of the shell: the tenant's
 * application logo (admin branding) when one is set, else the default sparkle
 * logo over the "Lumen / Copilot" wordmark. A link back to the Assistant home (`/`).
 *
 * The per-tenant `logo_url` comes from the principal the app already loads
 * (GET /auth/me, via `useCurrentUser`) — the same source the tenant pill uses.
 * When present it renders as an `<img>` in place of the default mark; while the
 * principal is loading or on error (or when no logo is set) the default sparkle
 * mark renders, so the chrome never shows a blank brand cell.
 */
import { Link } from 'react-router-dom';
import { Icon } from '@/ui';
import { useCurrentUser } from '@/features/auth';

export function Brand() {
  const me = useCurrentUser();
  const logoUrl = me.data?.logo_url ?? null;

  return (
    <Link to="/" className="lc-shell__brand" aria-label="Lumen Copilot — home">
      <span className="lc-brand__logo" aria-hidden="true">
        {logoUrl ? (
          <img src={logoUrl} alt="" className="lc-brand__logo-img" />
        ) : (
          <Icon name="sparkles" />
        )}
      </span>
      <span className="lc-brand__text">
        <span className="lc-brand__name">Lumen</span>
        <span className="lc-brand__sub">Copilot</span>
      </span>
    </Link>
  );
}
