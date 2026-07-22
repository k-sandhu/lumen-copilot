import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("https://backend-flow.test/", {
      headers: { accept: "text/html", host: "backend-flow.test" },
    }),
    {
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the endpoint explorer with site-specific metadata", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Lumen Backend Explorer<\/title>/i);
  assert.match(html, /What happens after/);
  assert.match(html, /\/api\/v1\/chat\/sessions\/:id\/messages/);
  assert.match(html, /ChatRuntime\.run/);
  assert.match(html, /RedisBackplane\.publish/);
  assert.match(html, /https:\/\/backend-flow\.test\/og\.png/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape/);
});

test("keeps the requested interaction and responsive affordances in the standalone page", async () => {
  const [page, css, hosting] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../.openai/hosting.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /onPointerDown/);
  assert.match(page, /onWheel/);
  assert.match(page, /ArrowRight/);
  assert.match(page, /Play trace/);
  assert.match(page, /Full flow/);
  assert.match(page, /Call stack/);
  assert.match(page, /Containers/);
  assert.match(page, /Boundary guarantee/);
  assert.match(css, /@media \(max-width: 760px\)/);
  assert.match(css, /prefers-reduced-motion/);
  const hostingConfig = JSON.parse(hosting);
  assert.match(hostingConfig.project_id, /^appgprj_/);
  assert.equal(hostingConfig.d1, null);
  assert.equal(hostingConfig.r2, null);
});
