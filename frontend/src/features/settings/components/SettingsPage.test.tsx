/**
 * SettingsPage state coverage — the user settings page (default model + custom
 * instructions + profile avatar). Covers the load-bearing behaviours
 * (frontend/AGENTS.md: not just success):
 *
 * - renders the three sections (Default model, Custom instructions, Profile picture);
 * - editing custom instructions and Save calls updatePreferences with the value;
 * - a blank custom-instructions Save sends null (clear);
 * - selecting an avatar and Upload calls updateAvatar(file);
 * - the current avatar image renders when /auth/me carries an avatar_url.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { ReactElement } from 'react';
import { renderWithQuery } from '@/test/renderWithQuery';
import type { CurrentUser, ModelList, UserPreferences } from '@/api';
import { SettingsPage } from './SettingsPage';

const getPreferences = vi.hoisted(() => vi.fn());
const updatePreferences = vi.hoisted(() => vi.fn());
const getCurrentUser = vi.hoisted(() => vi.fn());
const updateAvatar = vi.hoisted(() => vi.fn());
const clearAvatar = vi.hoisted(() => vi.fn());
const listModels = vi.hoisted(() => vi.fn());
const hasAccessToken = vi.hoisted(() => vi.fn(() => true));

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    getPreferences,
    updatePreferences,
    getCurrentUser,
    updateAvatar,
    clearAvatar,
    listModels,
    hasAccessToken,
  };
});

const MODELS: ModelList = {
  items: [
    {
      id: 'anthropic/claude-opus-4.8',
      label: 'Claude Opus 4.8',
      provider: 'anthropic',
      tier: 'frontier',
      is_default: true,
    },
  ],
};

const PREFS: UserPreferences = {
  default_model_id: null,
  custom_instructions: null,
  updated_at: null,
};

const ME: CurrentUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'alice@acme.test',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  tenant_name: 'Acme',
  roles: ['member'],
  created_at: '2026-06-18T00:00:00Z',
  logo_url: null,
  avatar_url: null,
};

function renderPage(ui: ReactElement) {
  return renderWithQuery(<MemoryRouter>{ui}</MemoryRouter>);
}

beforeEach(() => {
  getPreferences.mockReset().mockResolvedValue(PREFS);
  updatePreferences.mockReset().mockResolvedValue(PREFS);
  getCurrentUser.mockReset().mockResolvedValue(ME);
  updateAvatar.mockReset().mockResolvedValue({ avatar_url: 'https://storage.test/a.png' });
  clearAvatar.mockReset().mockResolvedValue(undefined);
  listModels.mockReset().mockResolvedValue(MODELS);
  hasAccessToken.mockReturnValue(true);
});

function pngFile(name = 'me.png'): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: 'image/png' });
}

describe('SettingsPage', () => {
  it('renders the three settings sections', async () => {
    renderPage(<SettingsPage />);

    expect(
      await screen.findByRole('heading', { name: /custom instructions/i }),
    ).toBeInTheDocument();
    // "Default model" appears as a section heading.
    expect(screen.getByRole('heading', { name: /default model/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /profile picture/i })).toBeInTheDocument();
  });

  it('saving edited custom instructions calls updatePreferences with the value', async () => {
    renderPage(<SettingsPage />);
    await screen.findByRole('heading', { name: /custom instructions/i });

    const textarea = screen.getByRole('textbox', { name: /custom instructions/i });
    await userEvent.type(textarea, 'Be concise.');
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(updatePreferences).toHaveBeenCalledWith({ custom_instructions: 'Be concise.' });
    expect(await screen.findByText(/instructions saved/i)).toBeInTheDocument();
  });

  it('clearing custom instructions sends null', async () => {
    getPreferences.mockResolvedValue({ ...PREFS, custom_instructions: 'Old text.' });
    renderPage(<SettingsPage />);
    // Wait for the stored value to seed the textarea.
    const textarea = (await screen.findByRole('textbox', {
      name: /custom instructions/i,
    })) as HTMLTextAreaElement;
    expect(textarea.value).toBe('Old text.');

    await userEvent.clear(textarea);
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(updatePreferences).toHaveBeenCalledWith({ custom_instructions: null });
  });

  it('selecting an avatar and Upload calls updateAvatar with the file', async () => {
    renderPage(<SettingsPage />);
    await screen.findByRole('heading', { name: /profile picture/i });

    const file = pngFile();
    await userEvent.upload(screen.getByLabelText(/choose an image/i), file);
    expect(await screen.findByTestId('avatar-preview')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /^upload$/i }));
    expect(updateAvatar).toHaveBeenCalledWith(file);
    expect(await screen.findByText(/profile picture updated/i)).toBeInTheDocument();
  });

  it('renders the current avatar image when /auth/me carries an avatar_url', async () => {
    getCurrentUser.mockResolvedValue({ ...ME, avatar_url: 'https://storage.test/current.png' });
    renderPage(<SettingsPage />);

    const img = await screen.findByTestId('current-avatar');
    expect(img).toHaveAttribute('src', 'https://storage.test/current.png');
  });
});
