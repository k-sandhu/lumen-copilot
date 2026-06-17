# Lumen Copilot frontend — Vite dev server (ADR-0005 local run).
# Static SPA in production; in compose we run the Vite dev server with the source
# bind-mounted at /app and a named volume keeping /app/node_modules off the host.
FROM node:20-alpine

# Enable pnpm via corepack (pinned in package.json#packageManager).
RUN corepack enable

WORKDIR /app

# Install deps first for layer caching. The compose bind-mount overlays /app at
# runtime, but node_modules lives in a named volume, so these installed deps win.
COPY package.json pnpm-lock.yaml* ./
RUN pnpm install

# Copy the rest of the source (overlaid by the bind-mount in dev).
COPY . .

EXPOSE 5173

# Bind to 0.0.0.0 so the host can reach the dev server through the published port.
CMD ["pnpm", "dev", "--host", "0.0.0.0", "--port", "5173"]
