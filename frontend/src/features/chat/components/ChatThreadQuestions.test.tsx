/**
 * #495 AC-4 (negative) — clarifying-question wiring is unchanged by the render
 * hygiene refactor.
 *
 * The activation logic (`questionActive`) and the answered-choice highlight
 * (`chosenAnswer`, resolved from the FOLLOWING user turn) moved out of
 * `ChatThread`'s inline row map into the memoised `PersistedMessages`. This pins
 * that the behaviour is byte-identical: options are clickable ONLY on the last
 * message with nothing in flight, inert while a live turn streams, and an
 * answered question highlights the choice taken by the next user turn.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AskUserQuestion, Message } from '@/api';
import { ChatThread, type ChatThreadProps, type LiveAnswer } from './ChatThread';

const originalScrollIntoView = Element.prototype.scrollIntoView;
afterEach(() => {
  if (originalScrollIntoView) {
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: originalScrollIntoView,
    });
  } else {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  }
});
Object.defineProperty(Element.prototype, 'scrollIntoView', {
  configurable: true,
  value: vi.fn(),
});

function question(text: string, options: string[]): AskUserQuestion {
  return { question: text, options: options.map((label) => ({ label })), allow_free_text: true };
}

function questionMessage(id: string, q: AskUserQuestion): Message {
  return {
    id,
    session_id: 's',
    role: 'assistant',
    content: '',
    citations: [],
    created_at: '2026-06-18T00:01:00Z',
    question: q,
  };
}

function userMessage(id: string, content: string): Message {
  return {
    id,
    session_id: 's',
    role: 'user',
    content,
    citations: [],
    created_at: '2026-06-18T00:02:00Z',
  };
}

function live(): LiveAnswer {
  return {
    phase: 'streaming',
    text: 'partial answer',
    citations: [],
    tools: [],
    codeRuns: [],
    steps: [],
    narration: null,
    askUser: null,
    problem: null,
  };
}

function thread(overrides: Partial<ChatThreadProps> = {}) {
  const props: ChatThreadProps = {
    messages: [],
    isLoading: false,
    isError: false,
    error: null,
    onRetryLoad: () => {},
    live: null,
    onRetryStream: () => {},
    onOpenCitation: () => {},
    ...overrides,
  };
  return <ChatThread {...props} />;
}

describe('#495 AC-4 clarifying-question wiring (relocated into PersistedMessages)', () => {
  it('activates options only on the last message with nothing in flight', async () => {
    const onSendText = vi.fn();
    const user = userEvent.setup();
    render(
      thread({
        messages: [questionMessage('q1', question('Which region?', ['EMEA', 'APAC']))],
        onSendText,
        live: null,
        sendBusy: false,
      }),
    );
    const group = screen.getByRole('group', { name: 'Answer options' });
    const emea = within(group).getByRole('button', { name: /EMEA/ });
    expect(emea).toBeEnabled();
    await user.click(emea);
    expect(onSendText).toHaveBeenCalledWith('EMEA');
  });

  it('renders options inert while a live turn is streaming', () => {
    render(
      thread({
        messages: [questionMessage('q1', question('Which region?', ['EMEA', 'APAC']))],
        onSendText: vi.fn(),
        live: live(),
      }),
    );
    const group = screen.getByRole('group', { name: 'Answer options' });
    for (const btn of within(group).getAllByRole('button')) {
      expect(btn).toBeDisabled();
    }
  });

  it('highlights the chosen option from the following user turn (answered, inert)', () => {
    render(
      thread({
        messages: [
          questionMessage('q1', question('Which region?', ['EMEA', 'APAC'])),
          userMessage('u1', 'APAC'),
        ],
        onSendText: vi.fn(),
        live: null,
      }),
    );
    const group = screen.getByRole('group', { name: 'Answer options' });
    const apac = within(group).getByRole('button', { name: /APAC/ });
    const emea = within(group).getByRole('button', { name: /EMEA/ });
    // Not the last message → inert; the following user turn selects APAC.
    expect(apac).toBeDisabled();
    expect(apac).toHaveAttribute('aria-pressed', 'true');
    expect(emea).toHaveAttribute('aria-pressed', 'false');
  });
});
