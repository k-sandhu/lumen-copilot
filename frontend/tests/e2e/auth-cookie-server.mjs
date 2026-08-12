import { createServer } from 'node:http';

const port = 4174;
const MAX_ACTIVE_SESSIONS = 8;
const AUTH_SLOT_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const sessions = new Map();
let heldLogout = false;
let heldResponse = null;
let holdRefresh = false;
let heldRefreshes = [];
let holdLogin = false;
let heldLogins = [];
let requestLog = [];
let sequence = 0;
let maxSetCookieHeaders = 0;

function json(response, status, body, headers = {}) {
  const setCookies = headers['Set-Cookie'];
  maxSetCookieHeaders = Math.max(
    maxSetCookieHeaders,
    Array.isArray(setCookies) ? setCookies.length : setCookies ? 1 : 0,
  );
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

function deleteCookie(slot) {
  return setCookie(slot, '', 0);
}

function isAuthSlot(value) {
  return typeof value === 'string' && AUTH_SLOT_PATTERN.test(value);
}

function revokeExcessSessions(persona, newSlot, previousSlot) {
  const protectedSlots = new Set([newSlot]);
  const previous = sessions.get(previousSlot);
  if (previous && previous.persona === persona && !previous.revoked) {
    protectedSlots.add(previousSlot);
  }
  const active = [...sessions.entries()]
    .filter(([, session]) => session.persona === persona && !session.revoked)
    .sort((left, right) => left[1].created - right[1].created || left[0].localeCompare(right[0]));
  let excess = Math.max(0, active.length - MAX_ACTIVE_SESSIONS);
  const cleanup = [];
  for (const [slot, session] of active) {
    if (excess === 0) break;
    if (protectedSlots.has(slot)) continue;
    session.revoked = true;
    cleanup.push(slot);
    excess -= 1;
  }
  if (excess !== 0) throw new Error('bounded fixture could not preserve selected + new slots');
  return cleanup;
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
  for (const pending of heldRefreshes) pending.response.destroy();
  for (const pending of heldLogins) pending.response.destroy();
  holdRefresh = false;
  heldRefreshes = [];
  holdLogin = false;
  heldLogins = [];
  requestLog = [];
  sequence = 0;
  maxSetCookieHeaders = 0;
}

function rotateRefresh(pending) {
  const session = sessions.get(pending.slot);
  if (!session || session.revoked || pending.presented !== session.secret) return null;
  session.secret = `refresh-${session.persona}-${pending.slot}-${++sequence}`;
  return session;
}

function writeRefreshSuccess(pending, session) {
  pending.record.status = 200;
  json(
    pending.response,
    200,
    {
      access_token: `jwt-persona-${session.persona}`,
      token_type: 'bearer',
      expires_in: 900,
    },
    { 'Set-Cookie': setCookie(pending.slot, session.secret) },
  );
}

function refreshSuccess(pending) {
  const session = rotateRefresh(pending);
  if (!session) return false;
  writeRefreshSuccess(pending, session);
  return true;
}

function refreshFailure(pending, superseded = false) {
  pending.record.status = 401;
  json(pending.response, 401, {
    title: 'Unauthorized',
    status: 401,
    ...(superseded ? { code: 'refresh_superseded' } : {}),
  });
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
  if (url.pathname === '/__control__/hold-refresh') {
    holdRefresh = true;
    response.writeHead(204).end();
    return;
  }
  if (url.pathname === '/__control__/hold-login') {
    holdLogin = true;
    response.writeHead(204).end();
    return;
  }
  if (url.pathname === '/__control__/release-login') {
    holdLogin = false;
    for (const pending of heldLogins.splice(0)) pending();
    response.writeHead(204).end();
    return;
  }
  if (url.pathname === '/__control__/release-refresh') {
    if (heldRefreshes.length !== 2) {
      json(response, 409, { held: heldRefreshes.length });
      return;
    }
    const order = url.searchParams.get('order') ?? 'loser-first';
    const [winner, loser] = heldRefreshes;
    heldRefreshes = [];
    holdRefresh = false;
    const winningSession = rotateRefresh(winner);
    const sendWinner = () => {
      if (winningSession) writeRefreshSuccess(winner, winningSession);
      else refreshFailure(winner);
    };
    const sendLoser = () => refreshFailure(loser, true);
    if (order === 'winner-first') {
      sendWinner();
      setTimeout(sendLoser, 30);
    } else {
      sendLoser();
      setTimeout(sendWinner, 30);
    }
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
      heldRefreshes: heldRefreshes.length,
      heldLogins: heldLogins.length,
      maxSetCookieHeaders,
      requests: requestLog,
    });
    return;
  }

  const slot = request.headers['x-lumen-auth-slot'] ?? null;
  const record = {
    method: request.method,
    path: url.pathname,
    slot,
    cookieHeaderBytes: Buffer.byteLength(request.headers.cookie ?? '', 'utf8'),
    status: null,
  };
  requestLog.push(record);

  if (url.pathname === '/api/v1/auth/login' && request.method === 'POST') {
    const previousSlot = request.headers['x-lumen-previous-auth-slot'] ?? null;
    if (!isAuthSlot(slot) || (previousSlot !== null && !isAuthSlot(previousSlot))) {
      record.status = 422;
      json(response, 422, { title: 'Validation error', status: 422 });
      return;
    }
    if (sessions.has(slot)) {
      record.status = 409;
      json(response, 409, { title: 'Conflict', status: 409 });
      return;
    }
    const credentials = await body(request);
    const persona = credentials.email.startsWith('persona-a') ? 'a' : 'b';
    if (holdLogin && persona === 'a') {
      await new Promise((resolve) => heldLogins.push(resolve));
    }
    const secret = `refresh-${persona}-${slot}-${++sequence}`;
    sessions.set(slot, { persona, secret, revoked: false, created: sequence });
    const cleanup = revokeExcessSessions(persona, slot, previousSlot);
    record.status = 200;
    json(
      response,
      200,
      { access_token: `jwt-persona-${persona}`, token_type: 'bearer', expires_in: 900 },
      { 'Set-Cookie': [setCookie(slot, secret), ...cleanup.map(deleteCookie)] },
    );
    return;
  }

  if (url.pathname === '/api/v1/auth/refresh' && request.method === 'POST') {
    if (!isAuthSlot(slot)) {
      record.status = 422;
      json(response, 422, { title: 'Validation error', status: 422 });
      return;
    }
    const session = sessions.get(slot);
    const presented = cookies(request)[cookieName(slot)];
    const pending = { response, slot, presented, record };
    if (holdRefresh && heldRefreshes.length < 2) {
      heldRefreshes.push(pending);
      return;
    }
    if (!session || session.revoked) refreshFailure(pending);
    else if (presented !== session.secret) refreshFailure(pending, true);
    else refreshSuccess(pending);
    return;
  }

  if (url.pathname === '/api/v1/auth/logout' && request.method === 'POST') {
    const persona = personaFromBearer(request);
    const session = sessions.get(slot);
    if (!persona || !session || session.persona !== persona) {
      record.status = 401;
      json(response, 401, { title: 'Unauthorized', status: 401 });
      return;
    }
    session.revoked = true;
    if (heldLogout) {
      heldResponse = { response, slot };
      return;
    }
    record.status = 204;
    maxSetCookieHeaders = Math.max(maxSetCookieHeaders, 1);
    response.writeHead(204, { 'Set-Cookie': deleteCookie(slot) });
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
