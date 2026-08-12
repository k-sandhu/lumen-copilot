import { createServer } from 'node:http';

const port = 4174;
const sessions = new Map();
let heldLogout = false;
let heldResponse = null;
let requestLog = [];

function json(response, status, body, headers = {}) {
  response.writeHead(status, { 'Content-Type': 'application/json', ...headers });
  response.end(JSON.stringify(body));
}

function cookies(request) {
  return Object.fromEntries(
    (request.headers.cookie ?? '')
      .split(';')
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => {
        const index = part.indexOf('=');
        return [part.slice(0, index), part.slice(index + 1)];
      }),
  );
}

function cookieName(slot) {
  return `lumen_refresh_token_${slot}`;
}

function setCookie(slot, value, maxAge = 1_209_600) {
  return `${cookieName(slot)}=${value}; HttpOnly; Max-Age=${maxAge}; Path=/api/v1/auth; SameSite=Strict`;
}

function personaFromBearer(request) {
  const authorization = request.headers.authorization ?? '';
  return authorization === 'Bearer jwt-persona-a'
    ? 'a'
    : authorization === 'Bearer jwt-persona-b'
      ? 'b'
      : null;
}

function currentUser(persona) {
  return {
    id: `00000000-0000-0000-0000-00000000000${persona === 'a' ? '1' : '2'}`,
    tenant_id: `10000000-0000-0000-0000-00000000000${persona === 'a' ? '1' : '2'}`,
    tenant_name: `Persona ${persona.toUpperCase()} tenant`,
    email: `persona-${persona}@example.test`,
    roles: ['admin'],
    created_at: '2026-08-12T00:00:00Z',
    logo_url: null,
    avatar_url: null,
  };
}

async function body(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : null;
}

function reset() {
  sessions.clear();
  heldLogout = false;
  if (heldResponse) {
    heldResponse.response.destroy();
    heldResponse = null;
  }
  requestLog = [];
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host}`);
  if (url.pathname === '/__control__/reset') {
    reset();
    response.writeHead(204).end();
    return;
  }
  if (url.pathname === '/__control__/hold-logout') {
    heldLogout = true;
    response.writeHead(204).end();
    return;
  }
  if (url.pathname === '/__control__/release-logout') {
    heldLogout = false;
    if (heldResponse) {
      const { response: pending, slot } = heldResponse;
      heldResponse = null;
      pending.writeHead(204, { 'Set-Cookie': setCookie(slot, '', 0) });
      pending.end();
    }
    response.writeHead(204).end();
    return;
  }
  if (url.pathname === '/__control__/state') {
    json(response, 200, {
      active: [...sessions.entries()]
        .filter(([, session]) => !session.revoked)
        .map(([slot, session]) => ({ slot, persona: session.persona })),
      heldLogout: heldResponse !== null,
      requests: requestLog,
    });
    return;
  }

  const slot = request.headers['x-lumen-auth-slot'] ?? null;
  requestLog.push({ method: request.method, path: url.pathname, slot });

  if (url.pathname === '/api/v1/auth/login' && request.method === 'POST') {
    const credentials = await body(request);
    const persona = credentials.email.startsWith('persona-a') ? 'a' : 'b';
    const secret = `refresh-${persona}-${slot}`;
    sessions.set(slot, { persona, secret, revoked: false });
    json(
      response,
      200,
      { access_token: `jwt-persona-${persona}`, token_type: 'bearer', expires_in: 900 },
      { 'Set-Cookie': setCookie(slot, secret) },
    );
    return;
  }

  if (url.pathname === '/api/v1/auth/refresh' && request.method === 'POST') {
    const session = sessions.get(slot);
    const presented = cookies(request)[cookieName(slot)];
    if (!session || session.revoked || presented !== session.secret) {
      json(response, 401, { title: 'Unauthorized', status: 401 });
      return;
    }
    session.secret = `${session.secret}-rotated`;
    json(
      response,
      200,
      {
        access_token: `jwt-persona-${session.persona}`,
        token_type: 'bearer',
        expires_in: 900,
      },
      { 'Set-Cookie': setCookie(slot, session.secret) },
    );
    return;
  }

  if (url.pathname === '/api/v1/auth/logout' && request.method === 'POST') {
    const persona = personaFromBearer(request);
    const session = sessions.get(slot);
    if (!persona || !session || session.persona !== persona) {
      json(response, 401, { title: 'Unauthorized', status: 401 });
      return;
    }
    session.revoked = true;
    if (heldLogout) {
      heldResponse = { response, slot };
      return;
    }
    response.writeHead(204, { 'Set-Cookie': setCookie(slot, '', 0) });
    response.end();
    return;
  }

  if (url.pathname === '/api/v1/auth/me') {
    const persona = personaFromBearer(request);
    if (persona) json(response, 200, currentUser(persona));
    else json(response, 401, { title: 'Unauthorized', status: 401 });
    return;
  }

  if (url.pathname === '/api/v1/admin/members') {
    json(response, 200, { items: [], next_cursor: null });
    return;
  }
  if (url.pathname === '/api/v1/admin/groups') {
    json(response, 200, { items: [], next_cursor: null });
    return;
  }
  if (url.pathname === '/api/v1/admin/llm-providers') {
    json(response, 200, { items: [] });
    return;
  }

  json(response, 404, { title: 'Not found', status: 404 });
});

server.listen(port, '127.0.0.1');

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
