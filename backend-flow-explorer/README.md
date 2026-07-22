# Lumen Backend Explorer

A standalone interactive map of Lumen Copilot's complete backend surface: 112
contracted HTTP operations plus two WebSocket routes. The endpoint catalog is
generated from `contracts/openapi.yaml`, with the two realtime routes added from
their FastAPI source modules.

The deepest trace follows `POST /api/v1/chat/sessions/{sessionId}/messages` from
the React client through FastAPI, service orchestration, model/tool calls,
permissioned retrieval, persistence, Redis, and the authenticated WebSocket
return path. Every other endpoint has a selectable high-level flow grounded in
its contract operation, source module, handler, transport, and architectural
pattern (CRUD, retrieval, storage, OAuth, jobs, sandbox, governance, health, or
realtime).

## Interactions

- Select any node to inspect its file, method, responsibility, calls, return
  value, encapsulation layer, runtime, and boundary guarantee.
- Search or filter the 114-endpoint catalog and select an endpoint to rebuild
  the canvas around that operation.
- Play or step through the principal happy-path execution sequence.
- Drag to pan and use Control/Command + scroll or the toolbar to zoom.
- Switch focus between the full flow, the Python call stack, and external/data
  containers.
- Use `J`/`K` or the arrow keys to step and Space to play/pause.

## Local development

Requires Node.js 22.13 or newer.

```bash
npm install
npm run generate:endpoints
npm run dev
```

## Verification

```bash
npm test
npm run lint
npx tsc --noEmit
```

The visualization is explanatory static content generated from and traced
against this repository; it does not collect telemetry or call the product
backend.
