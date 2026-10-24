import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

for (const width of [354, 390, 1440]) {
  test(`Documents navigation and controls remain usable at ${width}px (#776)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const requests: URL[] = [];
    let releaseLoading: (() => void) | undefined;
    let releaseInitial: (() => void) | undefined;
    const filename = `${'Long-policy-filename-'.repeat(10)}.pdf`;
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
          items: ['Policies', 'Other collection'].map((name, i) => ({
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
        if (requests.length === 1)
          await new Promise<void>((release) => {
            releaseInitial = release;
          });
        if (url.searchParams.get('q') === 'empty') return json({ items: [], next_cursor: null });
        if (url.searchParams.get('q') === 'error')
          return route.fulfill({
            status: 503,
            contentType: 'application/problem+json',
            body: JSON.stringify({ title: 'Service unavailable', status: 503 }),
          });
        if (url.searchParams.get('q') === 'loading')
          await new Promise<void>((release) => {
            releaseLoading = release;
          });
        return json({
          items: [
            {
              id: 'd1',
              filename,
              mime_type: 'application/pdf',
              size_bytes: 1,
              collection_id: url.searchParams.get('collection_id'),
              owner_id: 'u1',
              status: 'ready',
              chunk_count: 1,
              created_at: '2026-10-01T00:00:00Z',
              updated_at: '2026-10-01T00:00:00Z',
            },
          ],
          next_cursor: null,
        });
      }
      if (path.endsWith('/preferences'))
        return json({ default_model_id: null, custom_instructions: null, updated_at: null });
      return json({ items: [], next_cursor: null });
    });
    const output = resolve('../output/playwright');
    await mkdir(output, { recursive: true });
    await page.goto('/documents');
    await expect(page.getByText('Loading documents…')).toBeAttached();
    await page.screenshot({ path: resolve(output, `776-initial-loading-${width}.png`) });
    releaseInitial?.();
    await expect(page.getByRole('button', { name: `Open ${filename}`, exact: true })).toBeVisible();
    const pane = page.locator('main').last();
    const measurement = await pane.evaluate((node) => ({
      main: node.getBoundingClientRect().width,
      outer: node.parentElement!.getBoundingClientRect().width,
      scroll: node.scrollWidth,
      page: document.documentElement.scrollWidth,
    }));
    console.info(JSON.stringify({ width, ...measurement }));
    await page.screenshot({
      path: resolve(output, `776-${process.env.LAYOUT_PHASE ?? 'after'}-${width}.png`),
    });
    if (width < 768) {
      expect(measurement.main).toBeGreaterThan(width * 0.9);
      const choose = page.getByRole('button', { name: /Choose collection/ });
      await expect(choose).toHaveAttribute('aria-expanded', 'false');
      await choose.focus();
      await choose.press('Enter');
      await expect(choose).toHaveAttribute('aria-expanded', 'true');
      await page.screenshot({ path: resolve(output, `776-collections-${width}.png`) });
      const other = page.getByRole('button', { name: /^Other collection/ });
      await other.focus();
      await other.press('Enter');
      await expect(choose).toBeFocused();
      await expect(choose).toHaveAttribute('aria-expanded', 'false');
      await expect(choose).toContainText('Other collection');
      await choose.click();
      await page.getByRole('button', { name: /^Policies/ }).press('Escape');
      await expect(choose).toBeFocused();
      await expect(choose).toHaveAttribute('aria-expanded', 'false');
    } else {
      await expect(page.getByRole('button', { name: /^Policies/ })).toBeVisible();
      expect(measurement.main).toBeGreaterThan(600);
      expect(measurement.outer - measurement.main).toBeCloseTo(288, 0);
    }
    expect(measurement.page).toBeLessThanOrEqual(width);
    if (width < 768) {
      const name = page.getByText(filename, { exact: true });
      await expect(name).toHaveAttribute('title', filename);
      expect(await name.evaluate((node) => node.getBoundingClientRect().width)).toBeLessThanOrEqual(
        192,
      );
      const viewport = page
        .getByRole('table')
        .locator('xpath=ancestor::*[@data-radix-scroll-area-viewport][1]');
      await viewport.evaluate((node) => {
        node.scrollLeft = node.scrollWidth;
      });
      await expect(
        page.getByRole('button', { name: `Delete ${filename}`, exact: true }),
      ).toBeInViewport();
      await viewport.evaluate((node) => {
        node.scrollLeft = 0;
      });
    }

    const upload = page.getByRole('button', { name: 'Choose files', exact: true });
    await expect(upload).toBeVisible();
    const fileChooser = page.waitForEvent('filechooser');
    await upload.click();
    await (await fileChooser).setFiles([]);
    const filter = page.getByRole('searchbox', { name: 'Filter documents by filename' });
    await filter.pressSequentially('policy', { delay: 20 });
    await expect(filter).toHaveValue('policy');
    await expect.poll(() => requests.at(-1)?.searchParams.get('q')).toBe('policy');
    expect(requests.filter((url) => url.searchParams.has('q'))).toHaveLength(1);
    await page.getByRole('button', { name: 'Failed', exact: true }).click();
    await expect.poll(() => requests.at(-1)?.searchParams.get('status')).toBe('failed');
    expect(await filter.evaluate((node) => node.getBoundingClientRect().width)).toBeGreaterThan(
      200,
    );
    await expect(page.getByRole('button', { name: `Open ${filename}`, exact: true })).toBeVisible();
    await filter.fill('empty');
    await expect(page.getByText('No documents match these filters.')).toBeVisible();
    await page.screenshot({ path: resolve(output, `776-empty-${width}.png`) });
    await filter.fill('error');
    await expect(page.getByText('Couldn’t load documents')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeInViewport();
    await page.screenshot({ path: resolve(output, `776-error-${width}.png`) });
    await filter.fill('loading');
    await expect(page.getByText('Refreshing…')).toBeVisible();
    await page.screenshot({ path: resolve(output, `776-loading-${width}.png`) });
    releaseLoading?.();
    await expect(page.getByRole('button', { name: `Open ${filename}`, exact: true })).toBeVisible();
  });
}
