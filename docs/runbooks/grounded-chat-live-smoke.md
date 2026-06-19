# Runbook — grounded-chat live smoke (#29)

> **What this proves.** The M1 headline end-to-end on the **real** stack: upload a
> document, ask a question, and get a **streamed, grounded** answer with a
> **clickable citation that resolves to the source passage** — permissioned,
> cited, auditable (spec [0003](../specs/0003-product-scope-and-mission.md) §3,
> spec [0004](../specs/0004-security-and-domain-invariants.md) INV-2/INV-3,
> ADR [0003](../architecture/0003-application-stack.md) §9).
>
> **Why it's a runbook, not a test.** The real loop needs a model provider key
> (`OPENROUTER_API_KEY`) and the running stack, so it cannot run in offline CI.
> The offline harness (`backend/tests/eval/`, `backend/tests/test_grounded_e2e.py`)
> proves the *wiring* deterministically; this is the human verification that the
> *real* product answers correctly. Tracking the CI eval job is OD-7 (out of #29).

The ports below are the ADR-[0005](../architecture/0005-local-run-and-developer-workflow.md)
defaults (the `471xx` block); override only the `*_PORT` in `.env` if they collide.

---

## 0. Prerequisites

- Docker + Docker Compose.
- An **OpenRouter API key** (`sk-or-...`). The chat answer **and** the embeddings
  both go through the LiteLLM gateway → OpenRouter; with a blank key the backend
  boots but every model call returns a 503 (by design).

## 1. Configure & bring up the stack

```bash
cp .env.example .env
# Edit .env and set your key (the only required edit):
#   OPENROUTER_API_KEY=sk-or-...
# Outside `local`, also set a strong JWT_SECRET (the dev default is refused).

docker compose up -d
```

Wait for convergence (Postgres/Redis/MinIO healthy, backend + worker up, frontend
serving). Verify the backend sees every dependency:

```bash
curl -s http://localhost:47181/health/ready | python -m json.tool
# expect each dependency reported reachable; overall status ok
```

Open the app at <http://localhost:47180>.

## 2. Seed a user

Self-service registration is out of scope; seed a dev user with the CLI (runs
against the compose Postgres). Run it inside the backend container so it uses the
container's `DATABASE_URL`:

```bash
docker compose exec backend \
  uv run python -m app.auth.seed \
    --email kw@acme.test --password devpassword --tenant Acme --role member
```

This is idempotent — re-running leaves an existing user unchanged.

## 3. Log in (get an access token)

Either log in through the SPA at <http://localhost:47180>, or via the API:

```bash
TOKEN=$(curl -s http://localhost:47181/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"kw@acme.test","password":"devpassword"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
echo "$TOKEN" | head -c 12; echo " ...(token acquired)"
```

## 4. Create a collection and upload a sample document

A small sample document with a checkable fact (you can use the eval corpus text or
your own):

```bash
cat > /tmp/tax-guide.txt <<'EOF'
US Federal Income Tax Guide for 2024.

The standard deduction for a single filer in 2024 is $14,600.
For married couples filing jointly, the standard deduction is $29,200.
The deadline to file a federal income tax return for the 2024 tax year is April 15, 2025.
EOF

COLLECTION_ID=$(curl -s http://localhost:47181/api/v1/collections \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Tax docs"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

DOC_ID=$(curl -s http://localhost:47181/api/v1/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F "collection_id=$COLLECTION_ID" \
  -F "file=@/tmp/tax-guide.txt;type=text/plain" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "document: $DOC_ID"
```

Wait for ingestion (the Celery worker parses → chunks → embeds → persists) to move
the document from `pending` to `ready`:

```bash
# Poll until status == ready (chunk_count > 0).
curl -s http://localhost:47181/api/v1/documents/$DOC_ID \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

> If it sticks at `pending`, check the worker: `docker compose logs -f worker`.
> If it ends `failed`, the document row carries the reason (parse/embed error).

## 5. Ask a question — observe a streamed grounded answer

**In the SPA (the human check):** open a chat, pick a model, and ask
*"What is the 2024 standard deduction for a single filer?"*. You should see:

- tokens **stream** into the conversation pane;
- a **citation** appear in the (separately scrollable) sources pane;
- clicking the citation **deep-links** to the cited passage in the document —
  `$14,600`, from `tax-guide.txt`;
- a question with no supporting source (e.g. *"What is the company's stock
  ticker?"*) yields an honest **"I couldn't find it"** with **no** citation.

**Via the API (scriptable):** create a session, send the question (202 →
`stream_id`), connect the WS to watch the stream, then reload history to confirm
the citation persisted and resolves.

```bash
SESSION_ID=$(curl -s http://localhost:47181/api/v1/chat/sessions \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"smoke"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 202 send → returns the persisted user message + the stream_id.
STREAM_ID=$(curl -s http://localhost:47181/api/v1/chat/sessions/$SESSION_ID/messages \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"content":"What is the 2024 standard deduction for a single filer?"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["stream_id"])')

# Watch the streamed answer over the WS (start → delta… → event:citation → done).
#   ws://localhost:47181/ws/chat/$STREAM_ID?token=$TOKEN
# Use any WS client, e.g. websocat:
#   websocat "ws://localhost:47181/ws/chat/$STREAM_ID?token=$TOKEN"
```

Confirm the citation persisted and is resolvable via history (the `GET
.../messages` path the SPA renders from):

```bash
curl -s http://localhost:47181/api/v1/chat/sessions/$SESSION_ID/messages \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
# The assistant message should carry citations[] with the source document_name,
# chunk_id, and char_start/char_end that deep-link into the document.
```

**Pass criteria (the human verification):**

1. The answer contains the fact from the document (`$14,600`).
2. The assistant message carries **at least one citation** whose
   `document_name` is the uploaded file and whose `char_start`/`char_end`
   resolve to the supporting passage.
3. An unanswerable question returns an honest refusal with **zero** citations.

## 6. Run the live eval (answer-quality, real model + real pgvector)

The same golden Q/A-with-source set the offline harness scores, but against the
real model + the real pgvector hybrid. It seeds an **isolated schema** and drops
it on teardown, so it never touches app data.

```bash
# With the stack up and the key exported on the host:
export OPENROUTER_API_KEY=sk-or-...
export DATABASE_URL=postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen

cd backend
uv run --extra dev pytest tests/eval/test_eval_live.py -v
# Skips cleanly if the key or Postgres is absent.
```

It asserts the live thresholds (`Thresholds.live()`): groundedness,
citation-correctness, and retrieval-recall all clear the bar — i.e. the real loop
is grounded and correctly cited, not just wired.

## 7. Tear down

```bash
docker compose down        # keep volumes
docker compose down -v     # clean reset (drops Postgres/MinIO data)
```

---

## Audit trail (optional check)

Every retrieval + answer emits a product-audit event (spec 0004 §2.4, INV-6):
`retrieval.query` and `answer.generated`, the latter carrying the model id and the
citation count. A `security`-role user can read the trail; this confirms trust is
provable after the fact, not assumed.
