/**
 * Public surface of the auth feature slice (issue #48). Other features and the
 * routes import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports).
 */
export { RouteGuard } from './components/RouteGuard';
export { LoginScreen } from './components/LoginScreen';
export { CurrentUserMenu } from './components/CurrentUserMenu';
export { useAuthStore } from './model/authStore';
export type { AuthStatus } from './model/authStore';
export { useCurrentUser, useLogin, useLogout, currentUserQueryKey } from './model/queries';
export { useBootstrapSession } from './model/useBootstrapSession';
