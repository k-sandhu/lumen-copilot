import { afterEach, expect, it } from 'vitest';
import { screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelPicker, useChatStore } from './controls';
import { useChatStore as fullStore } from './index';
import { useChatStore as existingStore } from './model/chatStore';

const models = [
  { id: 'fast', label: 'Fast model', provider: 'test', tier: 'fast' as const, is_default: true },
  {
    id: 'frontier',
    label: 'Frontier model',
    provider: 'test',
    tier: 'frontier' as const,
    is_default: false,
  },
];
afterEach(() => {
  useChatStore.getState().closeSession();
  useChatStore.getState().setPendingModel(null);
});

it('uses the same store instance as the existing public chat surface', () => {
  expect(useChatStore).toBe(fullStore);
  expect(useChatStore).toBe(existingStore);
  useChatStore.getState().openSession('assistant-session');
  expect(fullStore.getState().activeSessionId).toBe('assistant-session');
  fullStore.getState().openSession('another-session');
  expect(useChatStore.getState().activeSessionId).toBe('another-session');
});

it('keeps ModelPicker labelled, controlled, and independent from global model state', async () => {
  const user = userEvent.setup();
  const changes: string[] = [];
  useChatStore.getState().setPendingModel('chat-only-override');
  const { rerender } = render(
    <ModelPicker models={models} value="fast" onChange={(value) => changes.push(value)} />,
  );
  const picker = screen.getByRole('combobox', { name: 'Model' });
  await user.selectOptions(picker, 'frontier');
  expect(changes).toEqual(['frontier']);
  expect(useChatStore.getState().pendingModel).toBe('chat-only-override');
  rerender(
    <ModelPicker
      models={models}
      value="frontier"
      onChange={(value) => changes.push(value)}
      disabled
    />,
  );
  expect(picker).toHaveValue('frontier');
  expect(picker).toBeDisabled();
});
