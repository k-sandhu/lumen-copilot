/**
 * `/settings` — the user settings page: an expanded version of the top-right account
 * popover. Three self-contained sections, each owning its own loading / empty / error
 * / saving states (frontend/AGENTS.md: every state, not just success):
 *
 *   1. Default model     — reuses the preferences slice's `DefaultModelSetting`.
 *   2. Custom instructions — a per-user preamble prepended to the chat system prompt.
 *   3. Profile picture     — the per-user avatar shown in the shell.
 *
 * The page nests inside the shell-aware PageChrome, so the top bar / nav / account
 * menu / theme are the SHELL's; this file refines CONTENT only. The body scrolls
 * independently of the pinned shell chrome (ScrollArea), and each section reads/writes
 * authenticated, caller-scoped data through the api/ boundary; a 401 (INV-4) surfaces
 * as an actionable per-section error rather than a blank screen.
 */
import { PageChrome } from '@/components/PageChrome';
import { ScrollArea } from '@/components/ScrollArea';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { DefaultModelSetting } from '@/features/preferences';
import { CustomInstructionsSetting } from './CustomInstructionsSetting';
import { AvatarSetting } from './AvatarSetting';

export function SettingsPage() {
  return (
    <PageChrome title="Settings">
      <ScrollArea viewportClassName="px-6 py-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-6">
          <header>
            <h1 className="text-lg font-semibold text-foreground">Settings</h1>
            <p className="mt-0.5 text-sm text-foreground-muted">
              Your account preferences — synced across your devices.
            </p>
          </header>

          <section
            aria-labelledby="settings-model-heading"
            className="rounded-lg border border-border"
          >
            <header className="border-b border-border px-4 py-3">
              <h2 id="settings-model-heading" className="text-sm font-semibold text-foreground">
                Default model
              </h2>
              <p className="mt-0.5 text-xs text-foreground-muted">
                Used for new chats. Per-turn model choice is still available in chat.
              </p>
            </header>
            <div className="p-4">
              <ErrorBoundary label="Default model">
                <DefaultModelSetting />
              </ErrorBoundary>
            </div>
          </section>

          <ErrorBoundary label="Custom instructions">
            <CustomInstructionsSetting />
          </ErrorBoundary>

          <ErrorBoundary label="Profile picture">
            <AvatarSetting />
          </ErrorBoundary>
        </div>
      </ScrollArea>
    </PageChrome>
  );
}
