/**
 * Brand cell (issue #110) — the top-left identity of the shell: a sparkle logo
 * over the "Lumen / Copilot" wordmark, matching docs/wireframes (buildChrome) on
 * production tokens. A link back to the Assistant home (`/`).
 */
import { Link } from 'react-router-dom';
import { Icon } from '@/ui';

export function Brand() {
  return (
    <Link to="/" className="lc-shell__brand" aria-label="Lumen Copilot — home">
      <span className="lc-brand__logo" aria-hidden="true">
        <Icon name="sparkles" />
      </span>
      <span className="lc-brand__text">
        <span className="lc-brand__name">Lumen</span>
        <span className="lc-brand__sub">Copilot</span>
      </span>
    </Link>
  );
}
