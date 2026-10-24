import { expect, test } from '@playwright/test';

test('Assistants loads controls without Markdown; actual Start chat loads the chat renderer (#531)', async ({
  page,
}) => {
  const scripts: string[] = [];
  const starts: unknown[] = [];
  const model = {
    id: 'fast',
    label: 'Fast model',
    provider: 'test',
    tier: 'fast',
    is_default: true,
  };
  const assistant = {
    id: 'a1',
    name: 'Benefits helper',
    description: 'Policy answers',
    instructions: null,
    model: 'fast',
    knowledgeScope: { collectionIds: [], sourceIds: [], modes: ['company'] },
    toolAllowlist: ['search_text'],
    autonomyLevel: 'suggest',
    effectiveAutonomy: 'suggest',
    owner: 'u1',
    backupOwner: 'u2',
    status: 'published',
    certificationState: 'none',
    featured: false,
    version: 1,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  };
  const session = {
    id: 's1',
    title: 'Benefits helper',
    model: 'fast',
    owner_id: 'u1',
    message_count: 0,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  };
  page.on('request', (request) => {
    if (request.resourceType() === 'script') scripts.push(new URL(request.url()).pathname);
  });
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown) =>
      route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/refresh'))
      return json({ access_token: 'test-token', token_type: 'bearer', expires_in: 900 });
    if (path.endsWith('/auth/me'))
      return json({
        id: 'u1',
        tenant_id: 't1',
        tenant_name: 'Test tenant',
        email: 'browser@example.test',
        roles: ['admin'],
        created_at: '2026-10-01T00:00:00Z',
      });
    if (path.endsWith('/models'))
      return json({
        items: [
          model,
          {
            ...model,
            id: 'frontier',
            label: 'Frontier model',
            tier: 'frontier',
            is_default: false,
          },
        ],
      });
    if (path.endsWith('/preferences'))
      return json({ default_model_id: null, custom_instructions: null, updated_at: null });
    if (path.endsWith('/assistants/a1')) return json(assistant);
    if (path.endsWith('/assistants')) return json({ items: [assistant], next_cursor: null });
    if (path.endsWith('/chat/sessions') && request.method() === 'POST') {
      starts.push(request.postDataJSON());
      return json(session);
    }
    if (path.endsWith('/chat/sessions'))
      return json({ items: starts.length ? [session] : [], next_cursor: null });
    if (path.endsWith('/chat/sessions/s1')) return json(session);
    return json({ items: [], next_cursor: null });
  });
  await page.goto('/assistants');
  await expect(page.getByRole('article', { name: 'Benefits helper' })).toBeVisible();
  const markdownScripts = () => scripts.filter((path) => /\/assets\/markdown-.*\.js$/.test(path));
  expect(markdownScripts()).toEqual([]);
  await page.getByRole('link', { name: 'Edit', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Model', exact: true })).toHaveValue('fast');
  await page.getByRole('combobox', { name: 'Model', exact: true }).selectOption('frontier');
  await expect(page.getByRole('combobox', { name: 'Model', exact: true })).toHaveValue('frontier');
  expect(markdownScripts()).toEqual([]);
  await page.goBack();
  await page.getByRole('button', { name: 'Start chat', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible();
  expect(starts).toEqual([{ assistant_id: 'a1' }]);
  await expect(page.getByRole('combobox', { name: 'Model', exact: true })).toHaveValue('fast');
  expect(markdownScripts().length).toBeGreaterThan(0);
});
