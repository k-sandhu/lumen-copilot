# Runbook — enabling `run_python` code execution (#503, #504)

> **What this covers.** Turning the code-execution sandbox on for a deploy, and
> what the sandbox can and cannot do once it is on: which image model-authored
> Python runs in, which packages are usable, and why arbitrary `pip install` is
> not (ADR [0013](../architecture/0013-code-execution-sandbox.md),
> ADR [0020](../architecture/0020-reusable-root-sandbox-sessions.md)).
>
> **Why it's a runbook.** Two of the four things that must be true are *outside*
> the application: an image on the host Docker daemon and a running
> `sandbox-runner` service. Neither is something the app can fix for you, and
> until #503 the documented default named an image that could not do data work at
> all.

---

## 0. The four independent gates

A blocked run is always one of these. They are checked in this order and each is
reported by its own name in the run's `stderr` (issue #502):

| # | Gate | Where | Symptom when it refuses |
|---|---|---|---|
| 1 | `SANDBOX_ENABLED` — the deploy kill-switch | `.env`, needs an API + worker restart | "turned off for this deployment" |
| 2 | Tenant sandbox policy `enabled` | Admin → Code execution | "no sandbox policy" / "switched off in this workspace" |
| 3 | Tool policy for `run_python` | Admin → Tools; the assistant's allow-list (builder → *Tools*) | the tool is never offered to the model |
| 4 | `sandbox-runner` reachable | compose `sandbox` profile | "the code sandbox service is unreachable" |

Gate 4 also covers **a missing execution image**: the runner cannot launch what is
not on the host daemon. Build it first (§1).

## 1. Build the execution image (do this BEFORE enabling)

`sandbox_exec/` builds `lumen-sandbox-exec` — the image model code runs **in**. It
is deliberately *not* `lumen-sandbox-runner`, which is the small control plane that
holds the Docker socket. (Before #503, `SANDBOX_IMAGE` defaulted to the control-plane
image, so every run had fastapi and pydantic available and **no pandas, numpy, or
matplotlib** — the capability could not do the data work it exists for.)

The runner drives the **host** Docker daemon through the mounted socket, so the
image must exist **there** before code execution is enabled.

> **Correction (was wrong until this PR).** This runbook used to say "nothing pulls
> it from a registry". That was false: docker-py's `containers.run` catches
> `ImageNotFound` and **pulls**, so a typo'd or tampered `SANDBOX_IMAGE` became a
> registry fetch performed by the one process holding the Docker socket — and the
> fetched image then executed as contained root. The runner now resolves the image on
> the local daemon *first* and refuses with **503 "sandbox execution image is not
> present on the host daemon"** when it is missing. Getting the image onto the host is
> an operator step (`build`, or an explicit `docker pull` of the digest); the runner
> will not do it for you.

```bash
# From the repo root — the documented build command:
docker compose --profile sandbox build sandbox-exec-image

# Equivalent without compose:
docker build -t lumen-sandbox-exec:0.1.0 ./sandbox_exec

# Confirm the host daemon has it (this is the daemon the runner drives):
docker image inspect lumen-sandbox-exec:0.1.0 --format '{{.Id}}'
```

### Pin the reference the runner launches (not just the base layer)

`sandbox_exec/Dockerfile` pins its **base** by digest, but that is a build-time fact
about a layer. What tenant code actually executes in is whatever `SANDBOX_IMAGE`
resolves to **at launch**, so that value is validated at boot:

| `SANDBOX_IMAGE` | `ENVIRONMENT=local` | anything else, `SANDBOX_ENABLED=true` |
|---|---|---|
| `lumen-sandbox-exec:0.1.0` | accepted (trust-on-first-build) | **refused** — needs a digest |
| `lumen-sandbox-exec@sha256:<64 hex>` | accepted | accepted |
| `lumen-sandbox-exec:0.1.0@sha256:<64 hex>` | accepted | accepted |
| `lumen-sandbox-exec:latest` | **refused** | **refused** |
| `lumen-sandbox-exec` (bare name) | **refused** | **refused** |

A tag is mutable on the daemon: anyone who can retag it picks what model-authored
code runs in, with no deploy and no audit trail. In local dev the tag is accepted
because you built the image yourself, on this daemon, from this checkout — outside
local dev that assumption does not hold, so the digest is mandatory.

Outside local dev this means the image comes from a registry you pushed it to (a
purely local build has no `RepoDigests` entry until then), and — because the runner
never pulls (§1 above) — you `docker pull` that digest onto the **host** daemon
yourself before enabling code execution:

```bash
# Get the digest of the pushed image, then pin and pre-pull it on the sandbox host:
docker image inspect <registry>/lumen-sandbox-exec:0.1.0 --format '{{index .RepoDigests 0}}'
docker pull <registry>/lumen-sandbox-exec@sha256:<64 hex>   # on the host the runner drives
```

Smoke-test the image the way the runner launches it — uid 0, all capabilities
dropped, no network — and confirm headless matplotlib really works:

```bash
docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges:true --user 0:0 \
  lumen-sandbox-exec:0.1.0 python -c \
  "import matplotlib; matplotlib.use('Agg'); import pandas as pd, numpy as np, matplotlib.pyplot as plt; \
   df = pd.DataFrame({'x': np.arange(10), 'y': np.arange(10) ** 2}); \
   df.plot(x='x', y='y').figure.savefig('/workspace/chart.png'); print('ok', df.y.sum())"
```

**Two traps this build already handles** — both were real failures, not theory:

- **`/workspace` is 0777, not owned by a user.** `--cap-drop ALL` takes
  `CAP_DAC_OVERRIDE` away from uid 0 as well, so root obeys ordinary permission
  bits. With a `sandbox`-owned `/workspace`, the runner's first `mkdir -p` failed
  with EACCES *as root* and every run died as "sandbox preparation command failed".
- **`MPLCONFIGDIR=/opt/mplconfig`, world-writable, font cache warmed at build
  time.** Unset, matplotlib writes to `$HOME`; non-writable, it falls back to a
  temp directory and prints a warning that lands in the run's captured stderr.
  Under the ADR-0020 layout (writable rootfs) the pre-warmed cache is used as-is.
  Under a stricter read-only-rootfs layout it still works — matplotlib rebuilds the
  cache in `/tmp`, slower and with that warning.

## 2. Enable the deploy switch and start the runner

```bash
# .env
SANDBOX_ENABLED=true
# SANDBOX_IMAGE=lumen-sandbox-exec:0.1.0   # the default; must be tag- or digest-pinned
#                                          # (digest REQUIRED outside ENVIRONMENT=local)
# SANDBOX_RUNTIME=runsc                    # REQUIRED outside ENVIRONMENT=local (gVisor)

docker compose --profile sandbox up -d sandbox-runner
docker compose restart backend worker        # SANDBOX_ENABLED is read at startup
```

`sandbox-exec-image` is a build-and-verify service, not a daemon: it prints the
stack it ships and exits 0. An `Exited (0)` container is the expected steady state.

Outside `ENVIRONMENT=local`, `SANDBOX_ENABLED=true` with `SANDBOX_RUNTIME=runc`
**fails configuration validation at boot** (ADR-0020 §2) — root-capable sandboxes
require gVisor there.

`SANDBOX_RUNTIME` is read **twice, from the same `.env`**: the backend validates it
(gVisor mandatory outside local), and compose passes it to the `sandbox-runner`
service, which is what actually launches containers with it. That is deliberate — the
runner takes the runtime from **its own** configuration and ignores the `runtime`
field on an ensure request, so a caller cannot pick `runc` for one session on a
gVisor deploy. **Restart `sandbox-runner` after changing it**
(`docker compose --profile sandbox up -d sandbox-runner`); restarting only the
backend and worker leaves the old runtime in force, and the runner refuses to start
at all on a value that is neither `runc` nor `runsc`.

## 3. Enable it for a workspace and an assistant

1. **Admin → Code execution**: turn the tenant sandbox policy **on**.
2. **Admin → Tools**: enable `run_python` for the tenant. Leave *requires approval*
   **off** unless an interactive approval path exists (#501) — a tool marked as
   requiring approval is refused, not queued for a human.
3. Add `run_python` to the **assistant's allow-list**: open the assistant in the
   builder (Assistants → the assistant → *Tools*), tick **Run Python (code
   execution)**, Save, and Publish. It is `default_offered=False`, so no ad-hoc chat
   offers it — an assistant grant is the only way a model is offered the tool.

   The picker reads `GET /tools` (issue #505), so it shows every registered tool with
   its risk tier and *this tenant's* effective policy. `run_python` is flagged **T2 /
   side-effecting**; if step 2 was skipped the row reads *"Requires approval"*, and if
   an admin disabled the tool for the tenant the row reads **Unavailable** and cannot
   be ticked. Before #505 the picker was a hardcoded list of four retrieval tools, so
   this grant could only be made with a direct database write.

Changing the image later does **not** re-image live chat sandboxes: a sandbox keeps
the `image_digest` it was created with until it is **reset** (`POST
/chat/sessions/{id}/sandbox/reset`) or closed. After switching `SANDBOX_IMAGE`,
reset any chat that already has a sandbox.

## 4. What packages a run can use

**Pre-installed — always available, no install, no network.** The execution image
ships the ADR-0013 §3 stack, pinned in `sandbox_exec/requirements.txt`:

`numpy`, `pandas`, `matplotlib` (headless `Agg`), `openpyxl`, plus their pinned
transitive dependencies (`pillow`, `fonttools`, `contourpy`, `cycler`, `kiwisolver`,
`pyparsing`, `python-dateutil`, `six`, `packaging`, `et_xmlfile`).

`import pandas` just works, and so does an explicit
`run_python(packages=["pandas"])` — even though a new tenant's `allowed_packages`
list is **empty**. That list governs *installs*, and something already in the image
needs none (issue #504). `Settings.sandbox_preinstalled_packages` mirrors the
manifest, and `backend/tests/test_sandbox_exec_image.py` fails if the two drift, so
the config cannot quietly disagree with the image.

**Anything else is a real install, and it needs two things:**

1. the distribution on the tenant's `allowed_packages` list (Admin → Code
   execution) — deny entries always win, including over a pre-installed package;
2. **outbound network from the `sandbox-runner` service**, which downloads binary
   wheels into its own temp directory and copies them into the offline container
   (ADR-0020 §3).

**The limitation, stated plainly.** Model-authored code **never** gets a network
route — the execution container is created with `--network none`, always. The
per-tenant `egress_allowed` / `egress_allowlist` settings do **not** open one for it
under ADR-0020, and enabling them will not make `pip install` work from inside a
run. On a deploy where the runner itself cannot reach a package index (the intended
locked-down posture), **the pre-installed stack is the whole story**: a request for
anything else is refused with a message saying so. Widen the capability by curating
the image (`sandbox_exec/requirements.txt` + a version bump), not by opening egress.

One honest wrinkle: denying a pre-installed package (e.g. `matplotlib`) refuses a
`packages=["matplotlib"]` *request*, but the distribution is still importable
because it is baked into the image. To make it genuinely unavailable, build and
pin a custom execution image without it.

## 5. Verify a real run

Ask an assistant whose allow-list includes `run_python` something that forces
execution, e.g. *"Using Python, compute 5! and print it."* Then check the record:

```bash
docker exec lumen-copilot-postgres-1 psql -U lumen -d lumen -tAc \
  "select status, exit_code, left(stdout, 40), left(stderr, 120), image_digest
     from code_runs order by created_at desc limit 3;"
```

- `status=succeeded` with your output in `stdout` → all four gates are open.
- `status=denied` → read `stderr`; it names the gate (§0).
- `status=failed` with "the code sandbox service is unreachable" → the runner is
  down, or the execution image is missing from the host daemon (§1).

## 6. Verify it automatically — the live execution test (#506)

Everything in §5 is a human looking at a database row. The automated equivalent is
`backend/tests/test_sandbox_execution_live.py`, which runs **real Python in a real
container** through the real `HttpSandboxRunner` seam and asserts the real stdout.

> **Why it exists.** Every other sandbox test substitutes something for reality — a
> mock HTTP transport, a fake `SandboxRunner`, a fake Docker client, or the
> Dockerfile read as *text*. None of them ever executed a line of Python, which is
> exactly how the defects behind #501–#505 all shipped under a green suite.

It needs the `sandbox` compose profile up (§2) **and** the execution image built
(§1). One command, from the repo root:

```bash
docker compose exec backend sh -lc \
  "UV_PROJECT_ENVIRONMENT=/tmp/lumen-dev-venv RUN_LIVE=1 \
   uv run --frozen --extra dev pytest -q -p no:cacheprovider tests/test_sandbox_execution_live.py"
```

It runs from **inside the backend container** because ADR-0020 §2 gives the runner
no published host port — the compose network is the only way in, which is also
exactly how the worker reaches it. Two details are not optional:
`UV_PROJECT_ENVIRONMENT` keeps the dev venv out of the bind-mounted `/app`, where it
would otherwise land on the host checkout's own `backend/.venv`, and
`-p no:cacheprovider` keeps `.pytest_cache` out of that same mount. (Wrapping it in
`sh -lc "…"` also stops Git Bash on Windows from rewriting `/tmp/...` into a Windows
path.) To run it from the host instead, point `SANDBOX_RUNNER_URL` at a forwarded
port.

What it covers, all against the live runner:

| Case | Asserts |
|---|---|
| stdlib | exact stdout, `exit_code=0`, empty stderr, a measured duration |
| pandas + numpy | the ADR-0013 §3 stack computes (#503) |
| control-plane absence | `fastapi` / the Docker SDK are **not** importable — proof the run is in `lumen-sandbox-exec`, not `lumen-sandbox-runner` (#503) |
| matplotlib | backend is `Agg`, no font-cache warning on stderr, and the rendered PNG comes back through output collection |
| **negative** — egress | a raw IP, a DNS name and the metadata IP all fail closed (`--network none`, ADR-0013 §5 G2–G4) |
| **negative** — crash | a raising script is reported `failed` with exit 1 and its traceback |
| session reuse | a second execution in one generation sees the first one's files (ADR-0020 §4) |

**It is off by default and never passes silently.** The opt-in is a strict
`RUN_LIVE=1` (only that exact string), and reachability is probed inside a fixture,
so a default `uv run --extra dev pytest` opens no socket and starts no container. If
you opt in and the runner is not there, it **skips with a reason naming the URL and
the command to start it** — it never reports green. If the runner answers but cannot
launch the image, that is a hard **failure** pointing at §1, because a missing
execution image is the #503 defect itself.

`backend/tests/test_sandbox_execution_gating.py` guards that gate offline (strict
opt-in parse, no import-time socket, every live test marked, and a named skip when
the runner is absent). It needs no Docker and runs in the normal suite.

What it does **not** cover, so read it honestly: the policy layers above the runner
(§0 gates 1–3), and the persistence of a `code_runs` row. Those have their own
offline tests; §5 remains the end-to-end check through chat.

## 7. Bump the execution image

1. Edit `sandbox_exec/requirements.txt` (keep every entry a `==` pin).
2. Bump the tag in `docker-compose.yml` (`sandbox-exec-image`) and the
   `SANDBOX_IMAGE` default in `backend/app/core/config.py`.
3. Mirror the manifest into `_DEFAULT_SANDBOX_PREINSTALLED_PACKAGES` in the same
   file — the drift test fails until you do.
4. Rebuild (§1), then **reset** existing chat sandboxes so they pick it up.
