/**
 * AssistantCard (#212) — one library card. Asserts it surfaces name, status,
 * resolved owner + model labels, and tool badges; that Edit links to the editor;
 * that "Start chat" is a keyboard-reachable button wired to its callback and
 * disabled for a disabled assistant; and that a per-card start error renders.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { Assistant, ChatModelInfo, Member } from '@/api';
import { AssistantCard } from './AssistantCard';

const models: ChatModelInfo[] = [
  { id: 'anthropic/claude', label: 'Claude', provider: 'anthropic', tier: 'frontier', is_default: true },
];
const members: Member[] = [{ id: 'u1', email: 'ada@acme.com', role: ['member'], email_attested_at: null }];

function makeAssistant(overrides: Partial<Assistant> = {}): Assistant {
  return {
    id: 'a1',
    name: 'Benefits helper',
    description: 'Answers HR questions',
    instructions: null,
    model: 'anthropic/claude',
    knowledgeScope: { collectionIds: [], sourceIds: [], modes: [] },
    toolAllowlist: ['search_text', 'get_document'],
    autonomyLevel: 'suggest',
    effectiveAutonomy: 'suggest',
    owner: 'u1',
    backupOwner: 'u2',
    status: 'published',
    certificationState: 'none',
    featured: false,
    version: 1,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
}

function renderCard(props: Partial<Parameters<typeof AssistantCard>[0]> = {}) {
  return render(
    <MemoryRouter>
      <AssistantCard
        assistant={makeAssistant()}
        models={models}
        members={members}
        starting={false}
        onStart={() => {}}
        {...props}
      />
    </MemoryRouter>,
  );
}

describe('AssistantCard', () => {
  it('shows name, status, resolved owner + model, and tool badges', () => {
    renderCard();
    const card = screen.getByRole('article', { name: /benefits helper/i });
    expect(within(card).getByRole('heading', { name: /benefits helper/i })).toBeInTheDocument();
    expect(within(card).getByText(/published/i)).toBeInTheDocument();
    expect(within(card).getByText('ada@acme.com')).toBeInTheDocument();
    expect(within(card).getByText('Claude')).toBeInTheDocument();
    expect(within(card).getByText('search_text')).toBeInTheDocument();
  });

  it('resolves a null model to "Smart default"', () => {
    renderCard({ assistant: makeAssistant({ model: null }) });
    expect(screen.getByText('Smart default')).toBeInTheDocument();
  });

  it('shows the effective autonomy label (AC-3, #218)', () => {
    renderCard({
      assistant: makeAssistant({ autonomyLevel: 'act_auto', effectiveAutonomy: 'act_auto' }),
    });
    const card = screen.getByRole('article', { name: /benefits helper/i });
    expect(within(card).getByText('Act automatically')).toBeInTheDocument();
    // Not capped: no "capped" chip when effective == configured.
    expect(within(card).queryByText(/capped/i)).not.toBeInTheDocument();
  });

  it('flags a capped assistant with the effective (lowered) autonomy (AC-3, #218)', () => {
    // Configured act_auto but the tenant cap clamps it to draft — show the EFFECTIVE
    // level plus a "capped" note so the user sees how far the agent may actually act.
    renderCard({
      assistant: makeAssistant({ autonomyLevel: 'act_auto', effectiveAutonomy: 'draft' }),
    });
    const card = screen.getByRole('article', { name: /benefits helper/i });
    expect(within(card).getByText('Draft')).toBeInTheDocument();
    expect(within(card).getByText(/capped/i)).toBeInTheDocument();
  });

  it('links Edit to the editor route', () => {
    renderCard();
    expect(screen.getByRole('link', { name: /edit/i })).toHaveAttribute('href', '/assistants/a1');
  });

  it('fires onStart from a keyboard-reachable button', async () => {
    const onStart = vi.fn();
    const user = userEvent.setup();
    renderCard({ onStart });
    await user.click(screen.getByRole('button', { name: /start chat/i }));
    expect(onStart).toHaveBeenCalledWith(expect.objectContaining({ id: 'a1' }));
  });

  it('disables Start chat for a disabled assistant', () => {
    renderCard({ assistant: makeAssistant({ status: 'disabled' }), startDisabled: true });
    expect(screen.getByRole('button', { name: /start chat/i })).toBeDisabled();
  });

  it('renders a per-card start error', () => {
    renderCard({ startError: 'Could not start a chat with this assistant.' });
    expect(screen.getByRole('alert')).toHaveTextContent(/could not start a chat/i);
  });

  it('shows a Certified badge for a certified assistant (#217)', () => {
    renderCard({ assistant: makeAssistant({ certificationState: 'certified' }) });
    const card = screen.getByRole('article', { name: /benefits helper/i });
    expect(within(card).getByText(/certified/i)).toBeInTheDocument();
  });

  it('shows a Deprecated badge for a deprecated assistant (#217)', () => {
    renderCard({ assistant: makeAssistant({ certificationState: 'deprecated' }) });
    const card = screen.getByRole('article', { name: /benefits helper/i });
    expect(within(card).getByText(/deprecated/i)).toBeInTheDocument();
  });

  it('shows a Featured badge for a featured assistant (#217)', () => {
    renderCard({ assistant: makeAssistant({ featured: true }) });
    const card = screen.getByRole('article', { name: /benefits helper/i });
    expect(within(card).getByText(/featured/i)).toBeInTheDocument();
  });

  it('shows no certification badge for an un-reviewed assistant (#217)', () => {
    renderCard({ assistant: makeAssistant({ certificationState: 'none' }) });
    const card = screen.getByRole('article', { name: /benefits helper/i });
    expect(within(card).queryByText(/certified|deprecated/i)).not.toBeInTheDocument();
  });
});
