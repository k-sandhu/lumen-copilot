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
image must exist **there**; nothing pulls it from a registry.

```bash
# From the repo root — the documented build command:
docker compose --profile sandbox build sandbox-exec-image

# Equivalent without compose:
docker build -t lumen-sandbox-exec:0.1.0 ./sandbox_exec

# Confirm the host daemon has it (this is the daemon the runner drives):
docker image inspect lumen-sandbox-exec:0.1.0 --format '{{.Id}}'
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
# SANDBOX_IMAGE=lumen-sandbox-exec:0.1.0   # the default; set only to override
# SANDBOX_RUNTIME=runsc                    # REQUIRED outside ENVIRONMENT=local (gVisor)

docker compose --profile sandbox up -d sandbox-runner
docker compose restart backend worker        # SANDBOX_ENABLED is read at startup
```

`sandbox-exec-image` is a build-and-verify service, not a daemon: it prints the
stack it ships and exits 0. An `Exited (0)` container is the expected steady state.

Outside `ENVIRONMENT=local`, `SANDBOX_ENABLED=true` with `SANDBOX_RUNTIME=runc`
**fails configuration validation at boot** (ADR-0020 §2) — root-capable sandboxes
require gVisor there.

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

## 6. Bump the execution image

1. Edit `sandbox_exec/requirements.txt` (keep every entry a `==` pin).
2. Bump the tag in `docker-compose.yml` (`sandbox-exec-image`) and the
   `SANDBOX_IMAGE` default in `backend/app/core/config.py`.
3. Mirror the manifest into `_DEFAULT_SANDBOX_PREINSTALLED_PACKAGES` in the same
   file — the drift test fails until you do.
4. Rebuild (§1), then **reset** existing chat sandboxes so they pick it up.
