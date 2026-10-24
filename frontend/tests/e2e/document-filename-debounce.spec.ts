import { expect, test } from '@playwright/test';

test('filename burst sends one settled request; clear and scope changes cancel pending text (#530)', async ({
  page,
}) => {
  const requests: URL[] = [];
  const row = {
    id: 'd1',
    filename: 'handbook.pdf',
    mime_type: 'application/pdf',
    size_bytes: 1,
    collection_id: 'col-1',
    owner_id: 'u1',
    status: 'ready',
    chunk_count: 1,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  };
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
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
    if (path.endsWith('/collections'))
      return json({
        items: ['Policies', 'Other'].map((name, i) => ({
          id: `col-${i + 1}`,
          name,
          owner_id: 'u1',
          document_count: 1,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-01T00:00:00Z',
        })),
        next_cursor: null,
      });
    if (path.endsWith('/documents')) {
      requests.push(url);
      return json({
        items: [{ ...row, collection_id: url.searchParams.get('collection_id') }],
        next_cursor: null,
      });
    }
    if (path.endsWith('/preferences'))
      return json({ default_model_id: null, custom_instructions: null, updated_at: null });
    return json({ items: [], next_cursor: null });
  });
  await page.goto('/documents');
  const input = page.getByRole('searchbox', { name: 'Filter documents by filename' });
  await expect(page.getByText('handbook.pdf')).toBeVisible();
  const initial = requests.length;
  await input.pressSequentially('handbook', { delay: 20 });
  await expect(input).toHaveValue('handbook');
  await expect.poll(() => requests.length).toBe(initial + 1);
  expect(requests.slice(initial).map((url) => url.searchParams.get('q'))).toEqual(['handbook']);
  await input.fill('stale');
  await input.fill('');
  await expect.poll(() => requests.at(-1)?.searchParams.get('q')).toBeNull();
  await input.fill('current');
  await page.getByRole('button', { name: 'Failed', exact: true }).click();
  await expect.poll(() => requests.at(-1)?.searchParams.get('status')).toBe('failed');
  expect(requests.at(-1)?.searchParams.get('q')).toBe('current');
  await input.fill('next');
  await page.getByRole('button', { name: /^Other/ }).click();
  await expect.poll(() => requests.at(-1)?.searchParams.get('collection_id')).toBe('col-2');
  expect(requests.at(-1)?.searchParams.get('q')).toBe('next');
  // Wait beyond the debounce to catch an incorrectly retained old-scope timer.
  const finalCount = requests.length;
  await page.waitForTimeout(350);
  expect(requests).toHaveLength(finalCount);
  expect(requests.some((url) => url.searchParams.get('q') === 'stale')).toBe(false);
  expect(
    requests.some(
      (url) =>
        url.searchParams.get('q') === 'next' && url.searchParams.get('collection_id') === 'col-1',
    ),
  ).toBe(false);
  await expect(page.getByText('handbook.pdf')).toBeVisible();
});
