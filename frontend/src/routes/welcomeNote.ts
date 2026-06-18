/**
 * Sanitized-markdown welcome note rendered in the rail. Deliberately exercises
 * the markdown pipeline with a heading, list, table, link, inline + fenced code
 * (with a language for highlighting) so the renderer pattern is proven on the
 * skeleton — and long code lines demonstrate horizontal scroll inside a pane.
 */
export const WELCOME_NOTE = `## Beacon — skeleton

This is the **boots-and-is-alive** SPA shell. It proves the patterns the product
UI will reuse — it is *not* the product yet.

- Server state via **TanStack Query** (the status panel)
- A typed **WebSocket** client (the realtime badge)
- This note is rendered through the **sanitized markdown** pipeline

| Pattern | Where |
| --- | --- |
| api/ boundary | \`src/api/\` |
| markdown | \`src/lib/markdown.tsx\` |
| panes | \`src/routes/App.tsx\` |

Calls go same-origin through the Vite proxy, e.g. \`GET /health/ready\`:

\`\`\`ts
import { getReadiness } from '@/api';
const status = await getReadiness(); // -> ReadinessStatus { status, dependencies[] }
\`\`\`

See the [frontend contract](https://example.invalid/frontend-agents) for the full quality bar.
`;
