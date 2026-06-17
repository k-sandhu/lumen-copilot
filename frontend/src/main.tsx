/**
 * App entry — mounts the React Query provider + router. Side-effect CSS imports
 * (Tailwind layer + highlight.js theme) live here so they load once, globally.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';

import './styles/index.css';
// highlight.js theme for fenced code blocks rendered by lib/markdown.tsx.
import 'highlight.js/styles/github-dark.css';

import { queryClient } from './queryClient';
import { router } from './routes/router';
import { ErrorBoundary } from './components/ErrorBoundary';
import { syncThemeToDom } from './stores/ui';

// Reflect the persisted/system theme onto <html> before first paint.
syncThemeToDom();

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found');
}

createRoot(rootElement).render(
  <StrictMode>
    <ErrorBoundary label="Application">
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
