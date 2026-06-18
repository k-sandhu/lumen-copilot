import { createBrowserRouter } from 'react-router-dom';
import { App } from './App';
import { DocsRoute, FeaturesRoute, DocumentsRoute } from './devPageRoutes';

/**
 * Router. The chat shell (`App`) lives at `/`; the developer pages are SEPARATE
 * top-level routes (not nested under the shell) so they render as standalone
 * pages. The route components are lazy-loaded (see devPageRoutes) so the docs
 * bundle — every repo markdown file via import.meta.glob — and the catalog stay
 * out of the main app chunk.
 */
export const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
  },
  {
    // Splat matches both /docs and /docs/<slug…>.
    path: '/docs/*',
    element: <DocsRoute />,
  },
  {
    path: '/features',
    element: <FeaturesRoute />,
  },
  {
    // Collections + documents management (#49). Auth-gated inside the route.
    path: '/documents',
    element: <DocumentsRoute />,
  },
]);
