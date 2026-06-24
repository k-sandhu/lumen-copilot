/**
 * App entry — mounts the React Query provider + router. Side-effect CSS imports
 * (Tailwind layer + highlight.js theme) live here so they load once, globally.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';

import './styles/index.css';
// Design-token layer (the single source of truth for both Tailwind --c-* and the
// kit's derived colors) — imported globally so tokens resolve on every screen,
// including pre-auth login, regardless of which feature renders first (#130).
import './styles/tokens.css';
// highlight.js theme for fenced code blocks rendered by lib/markdown.tsx.
import 'highlight.js/styles/github-dark.css';

import { queryClient } from './queryClient';
import { router } from './routes/router';
import { ErrorBoundary } from './components/ErrorBoundary';
import { TooltipProvider } from './components/Tooltip';
import { syncThemeToDom } from './stores/ui';
import { applyAppearance, resolveMode, readStoredMode } from './ui';
import { installAuthRefresh } from './api';

// Reflect the persisted/system theme onto <html> before first paint.
syncThemeToDom();
// Activate the design-token theme (data-theme + data-mode) globally before first
// paint so BOTH token layers resolve correctly on every screen — including the
// pre-auth login screen, which previously rendered with no [data-mode] (#130).
applyAppearance(resolveMode(readStoredMode()));

// Wire the silent-refresh handler into the api/ client so any 401 triggers one
// refresh + retry before surfacing (issue #48, AC-2/AC-4). Must run before the
// first request — and before the auth store, which subscribes to the token.
installAuthRefresh();

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found');
}

createRoot(rootElement).render(
  <StrictMode>
    <ErrorBoundary label="Application">
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delayDuration={200}>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
