import { expect, test } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`clear grounded search and preserve history at ${width}px (#596)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    const searches: URL[] = [];
    const writes: string[] = [];
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const json = (body: unknown, status = 200) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (request.method() === 'DELETE') writes.push(path);
      if (path.endsWith('/auth/refresh') || path.endsWith('/auth/login')) {
        return json({ access_token: 'browser-test-token', token_type: 'bearer', expires_in: 900 });
      }
      if (path.endsWith('/auth/me'))
        return json({
          id: 'u1',
          tenant_id: 't1',
          tenant_name: 'Test tenant',
          email: 'browser@example.test',
          roles: ['admin'],
          created_at: '2026-10-01T00:00:00Z',
        });
      if (path.endsWith('/search/suggest')) return json({ suggestions: [] });
      if (path.endsWith('/search/recent'))
        return json({ items: [{ query: 'Earlier query', last_used_at: '2026-10-01T00:00:00Z' }] });
      if (path.endsWith('/saved-searches'))
        return json({
          items: [
            {
              id: 's1',
              name: 'Scoped search',
              query: 'missing',
              source: 'upload',
              owner_id: 'u1',
              created_at: 'x',
              updated_at: 'x',
            },
          ],
        });
      if (path.endsWith('/search')) {
        searches.push(url);
        return json({ query: url.searchParams.get('q'), results: [], hidden_count: 0 });
      }
      if (path.endsWith('/preferences'))
        return json({ default_model_id: null, custom_instructions: null, updated_at: null });
      return json({ items: [], next_cursor: null });
    });
    await page.goto('/search');
    const box = page.getByRole('combobox', { name: 'Search your sources' });
    await box.click();
    await page.getByRole('option', { name: /Scoped search/ }).click();
    await expect(page.getByText(/No results for/)).toBeVisible();
    expect(searches[0]!.searchParams.get('source')).toBe('upload');
    await page.getByRole('button', { name: 'Clear search', exact: true }).click();
    await expect(box).toBeFocused();
    await expect(box).toHaveValue('');
    await expect(
      page.getByText('Search across your connected sources and uploaded documents.'),
    ).toBeVisible();
    await expect(page.getByText(/No results for/)).toHaveCount(0);
    await expect(page.getByRole('option', { name: 'Earlier query' })).toBeVisible();
    await box.press('Enter');
    expect(searches).toHaveLength(1);
    await box.fill('second');
    await box.press('Enter');
    await expect(page.getByText(/No results for/)).toBeVisible();
    expect(searches).toHaveLength(2);
    expect(searches[1]!.searchParams.get('source')).toBeNull();
    await box.press('ControlOrMeta+A');
    await box.press('Backspace');
    await expect(
      page.getByText('Search across your connected sources and uploaded documents.'),
    ).toBeVisible();
    await box.fill('   ');
    await box.press('Enter');
    await expect(page.getByText(/No results for/)).toHaveCount(0);
    expect(searches).toHaveLength(2);
    expect(writes).toEqual([]);
    expect(new URL(page.url()).pathname).toBe('/search');
    expect(new URL(page.url()).search).toBe('');
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', width);
  });
}
