/**
 * SearchPage (#84) — the `/search` screen body. The app shell (issue #110) now
 * owns the chrome (brand + top bar + nav rail + theme + account) and the auth gate
 * lives in the LAYOUT route (`routes/router.tsx`), so this no longer renders its
 * own header, back-to-chat link, theme toggle, account menu, or a nested
 * `RouteGuard` — that was a duplicate top bar inside the shell.
 *
 * It nests through the shared shell-aware `PageChrome` (like Audit/Admin): inside
 * the shell PageChrome renders just the scrollable body; standalone it would keep
 * the pinned top bar. The slice still owns only its own files (ADR-0008 §1) — no
 * edit to routes/.
 */
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { SearchScreen } from './SearchScreen';

export function SearchPage() {
  return (
    <PageChrome title="Search">
      <ErrorBoundary label="Search">
        <SearchScreen />
      </ErrorBoundary>
    </PageChrome>
  );
}
