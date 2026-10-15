import { expect, test, type Page, type Route } from '@playwright/test';

const SESSION_ID = '00000000-0000-4000-8000-000000000041';
const USER_MESSAGE_ID = '00000000-0000-4000-8000-000000000042';
const ANSWER_MESSAGE_ID = '00000000-0000-4000-8000-000000000043';
const STREAM_ID = '00000000-0000-4000-8000-000000000044';
const DOCUMENT_ID = '00000000-0000-4000-8000-000000000051';
const CHUNK_ID = '00000000-0000-4000-8000-000000000052';
const MODEL_ID = 'test/model';
const SOURCE_TITLE = 'Security handbook.txt';
const SOURCE_SNIPPET = 'Access is reviewed every quarter.';
const QUESTION = 'How often is access reviewed?';
const ANSWER = 'Access is reviewed quarterly [S1].';
const REFUSAL_QUESTION = 'What is the private payroll password?';
const REFUSAL = 'I cannot help with that request.';

const citationRest = {
  id: '00000000-0000-4000-8000-000000000053',
  handle: 'S1',
  document_id: DOCUMENT_ID,
  document_name: SOURCE_TITLE,
  chunk_id: CHUNK_ID,
  snippet: SOURCE_SNIPPET,
  char_start: 0,
  char_end: SOURCE_SNIPPET.length,
};

const citationWs = {
  id: citationRest.id,
  handle: 'S1',
  documentId: DOCUMENT_ID,
  documentName: SOURCE_TITLE,
  chunkId: CHUNK_ID,
  snippet: SOURCE_SNIPPET,
  charStart: 0,
  charEnd: SOURCE_SNIPPET.length,
};

function userMessage(content: string) {
  return {
    id: USER_MESSAGE_ID,
    session_id: SESSION_ID,
    role: 'user',
    content,
    created_at: '2026-10-01T12:00:00Z',
  };
}

function assistantMessage(content: string, citations: (typeof citationRest)[] = []) {
  return {
    id: ANSWER_MESSAGE_ID,
    session_id: SESSION_ID,
    role: 'assistant',
    content,
    model: MODEL_ID,
    citations,
    created_at: '2026-10-01T12:00:01Z',
  };
}

async function installRestMocks(page: Page, refusal: boolean) {
  let messages: Record<string, unknown>[] = [];
  let reloadCount = 0;
  let sessionCreated = false;
  const unexpectedApiCalls: string[] = [];

  // Every application API request is fulfilled locally. No request can fall
  // through to the backend or a configured proxy target.
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (path.endsWith('/auth/login')) {
      await json({ access_token: 'browser-test-token', token_type: 'bearer', expires_in: 900 });
    } else if (path.endsWith('/auth/refresh')) {
      await json({ title: 'Unauthorized', status: 401 }, 401);
    } else if (path.endsWith('/auth/me')) {
      await json({
        id: '00000000-0000-4000-8000-000000000001',
        tenant_id: '00000000-0000-4000-8000-000000000002',
        tenant_name: 'Browser test tenant',
        email: 'browser@example.test',
        roles: ['admin'],
        created_at: '2026-10-01T00:00:00Z',
      });
    } else if (path.endsWith('/models')) {
      await json({
        items: [
          { id: MODEL_ID, label: 'Test model', provider: 'test', tier: 'fast', is_default: true },
        ],
      });
    } else if (path.endsWith('/preferences')) {
      await json({ default_model_id: MODEL_ID, custom_instructions: null, updated_at: null });
    } else if (path.endsWith('/chat/sessions') && request.method() === 'GET') {
      await json({
        items: sessionCreated
          ? [
              {
                id: SESSION_ID,
                title: 'Handle browser regression',
                model: MODEL_ID,
                owner_id: '00000000-0000-4000-8000-000000000001',
                message_count: messages.length,
                created_at: '2026-10-01T12:00:00Z',
                updated_at: '2026-10-01T12:00:01Z',
              },
            ]
          : [],
      });
    } else if (path.endsWith('/chat/sessions') && request.method() === 'POST') {
      sessionCreated = true;
      await json(
        {
          id: SESSION_ID,
          title: 'Handle browser regression',
          model: MODEL_ID,
          owner_id: '00000000-0000-4000-8000-000000000001',
          message_count: 0,
          created_at: '2026-10-01T12:00:00Z',
          updated_at: '2026-10-01T12:00:00Z',
        },
        201,
      );
    } else if (
      path.endsWith(`/chat/sessions/${SESSION_ID}/messages`) &&
      request.method() === 'POST'
    ) {
      const content = (request.postDataJSON() as { content: string }).content;
      messages = [userMessage(content)];
      await json({ message: messages[0], stream_id: STREAM_ID }, 202);
    } else if (
      path.endsWith(`/chat/sessions/${SESSION_ID}/messages`) &&
      request.method() === 'GET'
    ) {
      reloadCount += 1;
      await json({ items: messages, next_cursor: null });
    } else if (path.endsWith(`/chat/sessions/${SESSION_ID}/usage`)) {
      await json({
        model: MODEL_ID,
        totals: {
          answers: messages.length > 1 ? 1 : 0,
          prompt_tokens: 0,
          completion_tokens: 0,
          total_tokens: 0,
          cached_prompt_tokens: 0,
          cache_write_tokens: 0,
        },
        input_budget_tokens: 1000,
        window_known: true,
      });
    } else if (path.endsWith(`/chat/sessions/${SESSION_ID}/sandbox`)) {
      await json({
        status: 'not_created',
        enabled: true,
        root_access: true,
        sandbox_session_id: null,
        generation: null,
      });
    } else if (path.endsWith(`/documents/${DOCUMENT_ID}/text`)) {
      await json({ text: SOURCE_SNIPPET, chunk_count: 1, truncated: false });
    } else if (path.endsWith('/auth/logout')) {
      await route.fulfill({ status: 204 });
    } else {
      unexpectedApiCalls.push(`${request.method()} ${path}`);
      await json({ title: 'Not found', status: 404 }, 404);
    }
  });

  await page.route('**/api/v2/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/documents/${DOCUMENT_ID}/access-url`)) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          url: 'http://127.0.0.1:1/mock-object',
          filename: SOURCE_TITLE,
          mime_type: 'text/plain',
          size_bytes: SOURCE_SNIPPET.length,
          expires_at: '2026-10-01T13:00:00Z',
          purpose: 'preview',
          supports_byte_ranges: false,
        }),
      });
    } else {
      unexpectedApiCalls.push(`${route.request().method()} ${path}`);
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: '{"title":"Not found","status":404}',
      });
    }
  });

  await page.route('http://127.0.0.1:1/mock-object', (route) =>
    route.fulfill({ status: 200, contentType: 'text/plain', body: SOURCE_SNIPPET }),
  );

  return {
    getReloadCount: () => reloadCount,
    getUnexpectedApiCalls: () => unexpectedApiCalls,
    persistAnswer: () => {
      messages = [
        userMessage(refusal ? REFUSAL_QUESTION : QUESTION),
        assistantMessage(refusal ? REFUSAL : ANSWER, refusal ? [] : [citationRest]),
      ];
    },
  };
}

function streamEnvelope(type: string, seq: number, data?: unknown, name?: string) {
  return {
    type,
    streamId: STREAM_ID,
    seq,
    ...(name ? { name } : {}),
    ...(data !== undefined ? { data } : {}),
  };
}

async function signIn(page: Page, navigate = true) {
  if (navigate) await page.goto('/');
  await page.getByLabel(/email/i).fill('browser@example.test');
  await page.getByLabel(/password/i).fill('browser-test-password');
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
}

test('send → WS source handle → cited document → persisted history reload (#436)', async ({
  page,
}) => {
  const api = await installRestMocks(page, false);
  let finishStream: (() => void) | undefined;
  let streamHasStarted: (() => void) | undefined;
  const started = new Promise<void>((resolve) => {
    streamHasStarted = resolve;
  });
  await page.routeWebSocket(
    (url) => url.pathname.startsWith('/ws/chat/'),
    (socket) => {
      const handshake = new URL(socket.url());
      expect(handshake.pathname).toBe(`/ws/chat/${STREAM_ID}`);
      expect(handshake.searchParams.get('access_token')).toBe('browser-test-token');
      socket.send(
        JSON.stringify(
          streamEnvelope('start', 0, {
            sessionId: SESSION_ID,
            messageId: ANSWER_MESSAGE_ID,
            model: MODEL_ID,
          }),
        ),
      );
      socket.send(JSON.stringify(streamEnvelope('delta', 1, { text: ANSWER })));
      streamHasStarted?.();
      finishStream = () => {
        socket.send(JSON.stringify(streamEnvelope('event', 2, citationWs, 'citation')));
        api.persistAnswer();
        socket.send(
          JSON.stringify(
            streamEnvelope('done', 3, {
              messageId: ANSWER_MESSAGE_ID,
              finishReason: 'stop',
              citationCount: 1,
            }),
          ),
        );
      };
    },
  );

  await signIn(page);
  await page.getByRole('button', { name: 'New chat' }).click();
  await page.getByLabel('Message', { exact: true }).fill(QUESTION);
  await page.getByRole('button', { name: 'Send message' }).click();
  await started;
  await expect(page.getByText(ANSWER, { exact: true })).toBeVisible();
  expect(finishStream).toBeDefined();
  finishStream!();

  const inlineCitation = page.getByRole('button', { name: `Citation S1: ${SOURCE_TITLE}` });
  await expect(inlineCitation).toBeVisible();
  await expect(page.getByText(SOURCE_TITLE, { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Sources used').first()).toBeVisible();
  await expect.poll(api.getReloadCount).toBeGreaterThan(1);

  await inlineCitation.click();
  const sourcePanel = page.getByRole('region', { name: `Cited document: ${SOURCE_TITLE}` });
  await expect(sourcePanel).toBeVisible();
  await expect(sourcePanel.getByText(SOURCE_SNIPPET).first()).toBeVisible();

  await page.reload();
  await signIn(page, false);
  await page.getByRole('button', { name: 'Handle browser regression', exact: true }).click();
  await expect(page.getByRole('button', { name: `Citation S1: ${SOURCE_TITLE}` })).toBeVisible();
  await expect(page.getByText('Sources used').first()).toBeVisible();
  await page.getByRole('button', { name: `Citation S1: ${SOURCE_TITLE}` }).click();
  await expect(page.getByRole('region', { name: `Cited document: ${SOURCE_TITLE}` })).toBeVisible();
  expect(api.getUnexpectedApiCalls()).toEqual([]);
});

test('zero-citation refusal has no source UI (#436)', async ({ page }) => {
  const api = await installRestMocks(page, true);
  await page.routeWebSocket(
    (url) => url.pathname.startsWith('/ws/chat/'),
    (socket) => {
      const handshake = new URL(socket.url());
      expect(handshake.pathname).toBe(`/ws/chat/${STREAM_ID}`);
      expect(handshake.searchParams.get('access_token')).toBe('browser-test-token');
      socket.send(
        JSON.stringify(
          streamEnvelope('start', 0, {
            sessionId: SESSION_ID,
            messageId: ANSWER_MESSAGE_ID,
            model: MODEL_ID,
          }),
        ),
      );
      socket.send(JSON.stringify(streamEnvelope('delta', 1, { text: REFUSAL })));
      api.persistAnswer();
      socket.send(
        JSON.stringify(
          streamEnvelope('done', 2, {
            messageId: ANSWER_MESSAGE_ID,
            finishReason: 'stop',
            citationCount: 0,
          }),
        ),
      );
    },
  );

  await signIn(page);
  await page.getByRole('button', { name: 'New chat' }).click();
  await page.getByLabel('Message', { exact: true }).fill(REFUSAL_QUESTION);
  await page.getByRole('button', { name: 'Send message' }).click();

  await expect(page.getByText(REFUSAL, { exact: true })).toBeVisible();
  await expect(page.getByText('No sources were cited for this answer.')).toBeVisible();
  await expect(page.getByText('Sources used')).toHaveCount(0);
  await expect(page.getByRole('region', { name: /Cited document:/ })).toHaveCount(0);
  await page.reload();
  await signIn(page, false);
  await page.getByRole('button', { name: 'Handle browser regression', exact: true }).click();
  await expect(page.getByText(REFUSAL, { exact: true })).toBeVisible();
  await expect(page.getByText('Sources used')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Citation S/ })).toHaveCount(0);
  expect(api.getUnexpectedApiCalls()).toEqual([]);
});
