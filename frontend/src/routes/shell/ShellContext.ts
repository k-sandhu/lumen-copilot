/**
 * Shell context (issue #110) — a flag that tells screen-level chrome it is being
 * rendered INSIDE the app shell. Shared chrome (`components/PageChrome`) reads it
 * to suppress its own standalone header/back-link/theme-toggle when the shell
 * already provides them, so screens nest cleanly with no duplicate chrome. Outside
 * the shell (a dev page reached directly), the flag is false and the standalone
 * chrome renders as before.
 *
 * Lives in `routes/shell/` (shell-owned). `components/PageChrome` imports the
 * hook — a one-directional dependency on the shell, not the reverse.
 */
import { createContext, useContext } from 'react';

const InAppShellContext = createContext(false);

export const InAppShellProvider = InAppShellContext.Provider;

/** True when the current subtree is rendered inside the app shell. */
export function useInAppShell(): boolean {
  return useContext(InAppShellContext);
}
