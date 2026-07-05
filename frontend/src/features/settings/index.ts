/**
 * Public surface of the settings feature slice — the user settings page (`/settings`):
 * default model, custom instructions, and profile avatar. Routes and other modules
 * import from here, never from deep paths (frontend/AGENTS.md: no cross-feature deep
 * imports).
 */
export { SettingsPage } from './components/SettingsPage';
export { CustomInstructionsSetting } from './components/CustomInstructionsSetting';
export { AvatarSetting } from './components/AvatarSetting';
export { useUpdateAvatar, useClearAvatar } from './model/queries';
