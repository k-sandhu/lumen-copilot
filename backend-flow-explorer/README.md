# Lumen Backend Explorer

A standalone interactive map of a real Lumen Copilot backend flow. The first
slice follows `POST /api/v1/chat/sessions/{session_id}/messages` from the React
client through FastAPI, service orchestration, model/tool calls, permissioned
retrieval, persistence, Redis, and the authenticated WebSocket return path.

## Interactions

- Select any node to inspect its file, method, responsibility, calls, return
  value, encapsulation layer, runtime, and boundary guarantee.
- Play or step through the principal happy-path execution sequence.
- Drag to pan and use Control/Command + scroll or the toolbar to zoom.
- Switch focus between the full flow, the Python call stack, and external/data
  containers.
- Use `J`/`K` or the arrow keys to step and Space to play/pause.

## Local development

Requires Node.js 22.13 or newer.

```bash
npm install
npm run dev
```

## Verification

```bash
npm test
npm run lint
npx tsc --noEmit
```

The visualization is explanatory static content traced from Lumen Copilot main
at commit `38d4dcc`; it does not collect telemetry or call the product backend.
