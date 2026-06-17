import { createBrowserRouter } from 'react-router-dom';
import { App } from './App';

/**
 * Router. Single shell route today; feature routes (chat, sources) slot in as
 * children as they land. Kept separate from main.tsx so it's unit-testable.
 */
export const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
  },
]);
