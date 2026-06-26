/**
 * AppShell (issue #110) — the cohesive chrome that wraps EVERY authenticated
 * screen: a grid of brand cell + top bar + left nav rail + main. The routed
 * screen renders into `<Outlet/>` in the independently-scrollable main; the chrome
 * stays pinned. Structure/feel matches docs/wireframes (buildChrome) but is built
 * on the PRODUCTION token system (styles/tokens.css via @/ui) — no wireframe CSS
 * is copied verbatim.
 *
 * The omni bar opens the EXISTING command palette (`ui/CommandPalette` +
 * `useCommandPalette`); ⌘/Ctrl-K toggles it globally. The rail is collapsible to
 * an icons-only strip on desktop and an overlay on narrow widths, driven by the
 * top-bar toggle.
 *
 * Quality bar (ADR-0006): keyboard-navigable with focus-visible rings (kit
 * tokens), honors prefers-reduced-motion (global rule + token durations), rail and
 * main scroll independently (min-height:0 grid), and works in light + dark.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { CommandPalette, useCommandPalette } from '@/ui';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Brand } from './Brand';
import { TopBar } from './TopBar';
import { NavRail } from './NavRail';
import { useShellCommands } from './useShellCommands';
import { InAppShellProvider } from './ShellContext';
import './shell.css';

const MOBILE_QUERY = '(max-width: 880px)';

function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches,
  );
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const mql = window.matchMedia(MOBILE_QUERY);
    const onChange = (): void => setIsMobile(mql.matches);
    onChange();
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', onChange);
      return () => mql.removeEventListener('change', onChange);
    }
    mql.addListener(onChange);
    return () => mql.removeListener(onChange);
  }, []);
  return isMobile;
}

export function AppShell() {
  const palette = useCommandPalette();
  const commands = useShellCommands();
  const isMobile = useIsMobile();
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);

  // Desktop: collapse to an icons-only rail. Mobile: open/close an overlay rail.
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  // Leaving mobile width drops any open overlay so the desktop rail is correct.
  useEffect(() => {
    if (!isMobile) setMobileOpen(false);
  }, [isMobile]);

  // On route change, move focus to <main> (tabIndex=-1) and reset its scroll so
  // a keyboard/SR user lands on the new screen instead of the clicked nav link,
  // and the new screen starts from the top rather than the previous scroll spot.
  useEffect(() => {
    mainRef.current?.focus();
    mainRef.current?.scrollTo?.(0, 0);
  }, [location.pathname]);

  const toggleRail = useCallback(() => {
    if (isMobile) setMobileOpen((v) => !v);
    else setCollapsed((v) => !v);
  }, [isMobile]);

  const closeMobile = useCallback(() => setMobileOpen(false), []);

  const railState = isMobile
    ? mobileOpen
      ? 'open'
      : 'closed'
    : collapsed
      ? 'collapsed'
      : 'expanded';

  return (
    <div className="lc-shell" data-rail={railState}>
      {/* First focusable element on the page: lets keyboard users jump past the
          chrome straight to the routed screen. Off-screen until focused. */}
      <a href="#main-content" className="lc-skip">
        Skip to content
      </a>
      <Brand />
      <TopBar onOpenPalette={() => palette.setOpen(true)} onToggleRail={toggleRail} />
      <NavRail onNavigate={isMobile ? closeMobile : undefined} />

      {isMobile && mobileOpen ? (
        <button
          type="button"
          className="lc-shell__scrim"
          aria-label="Close navigation"
          onClick={closeMobile}
        />
      ) : null}

      <main className="lc-shell__main" id="main-content" ref={mainRef} tabIndex={-1}>
        <ErrorBoundary label="Screen">
          <InAppShellProvider value={true}>
            <Outlet />
          </InAppShellProvider>
        </ErrorBoundary>
      </main>

      <CommandPalette
        open={palette.open}
        onClose={() => palette.setOpen(false)}
        items={commands}
        placeholder="Search or ask across your workspace…"
      />
    </div>
  );
}
