# 13. Code-execution sandbox for agent-authored Python

- **Status:** Accepted, **superseded in part by [ADR-0020](0020-reusable-root-sandbox-sessions.md)** *(ADR-0020 replaces the fresh-container-per-run, non-root/read-only-rootfs, automatic timeout, and per-run resource-cap decisions for interactive `run_python`; the dedicated runner boundary and remaining isolation/audit decisions stay in force.)*
- **Date:** 2026-07-02
- **Builds on:** [ADR-0003](0003-application-stack.md) (OSS-only stack, Celery off the request path), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (module boundaries — a new owning module + a boundary-table row), [ADR-0005](0005-local-run-and-developer-workflow.md) (one-command laptop-viable compose, no `:latest`), [ADR-0008](0008-conflict-free-parallel-delivery.md) (serialized seam → parallel build), [ADR-0009](0009-connector-framework-and-web-source.md) (deny-by-default egress / SSRF stance mirrored here), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1 tenancy, INV-2 permission, INV-6 audit, INV-7 read-before-write; deny-by-default)

## Context

Epic [#200](https://github.com/k-sandhu/lumen-copilot/issues/200) lets an assistant **author and execute Python** to analyze data, compute results, and emit files (story **E15-7**; also **E3-7** conversational spreadsheet/dataset analysis, and it enriches **E8-3** metrics analysis and **E6-5** debuggable runs). Executing model-authored code on our infrastructure is **the highest-risk capability in the program** (`risk:security`): the code is adversarial-by-assumption, and a single escape reads other tenants' data, our secrets, or the host.

The spike question ([#204](https://github.com/k-sandhu/lumen-copilot/issues/204)): **what technology executes agent-authored Python with the isolation E15-7 requires — files, network, secrets, execution time — on the OSS-only, Docker-based stack** ([ADR-0003](0003-application-stack.md)/[ADR-0005](0005-local-run-and-developer-workflow.md))? This choice is **costly to reverse** (it sets the compose topology, the security posture, and the run/result contract three dependent features build on), which is why it is a spike that lands as an ADR before any code.

**Options evaluated** (from [#204](https://github.com/k-sandhu/lumen-copilot/issues/204)):

| Option | Isolation | Footprint | Verdict |
|---|---|---|---|
| **Container-per-run** — ephemeral Docker container: no network, read-only rootfs, tmpfs workdir, dropped caps, non-root, cpu/mem/pids limits, wall-clock kill | Strong-ish (kernel shared) | Medium | **Chosen (baseline).** Fits compose; Docker-socket access is confined to a **dedicated runner service**, never the app/worker (see §1). |
| **gVisor (`runsc`)** as the runtime *under* the container option | Strong (user-space kernel intercepts syscalls) | Medium | **Chosen (recommended production hardening).** Best isolation-per-effort; a drop-in OCI runtime swap over the same runner (see §2). |
| **Firecracker microVM** (via a runner) | Strongest (hardware virt) | Heavy | **Deferred — prod-only option.** Needs `/dev/kvm`; too heavy for the laptop compose floor ([ADR-0005](0005-local-run-and-developer-workflow.md)). Noted as a future runtime behind the same runner seam. |
| **nsjail / bubblewrap** wrapping a `python` subprocess | Medium (namespaces + seccomp, shared kernel, no image boundary) | Light | **Rejected as the primary boundary** (weaker than a container image + optional gVisor; still valuable as defense-in-depth *inside* the runner). |
| **WASM (Pyodide) / RestrictedPython** | Weak–moderate | Light | **Rejected as the primary mechanism** — pure-Python only, cannot run the compiled pandas/numpy/matplotlib stack E3-7 needs. |

The decision below is delegated to this spike by the sponsor within the epic's decided fences (OSS-only; one-command compose; off the request path; deny-by-default egress; a new owning module + boundary row). It defaults **no** open decision in [spec 0001](../specs/0001-open-decisions.md) (only OD-6/OD-7 remain open; neither is touched here).

## Decision

Execute agent-authored Python as a **container-per-run**, driven by a **dedicated `sandbox-runner` service** that the Celery worker calls over an **internal HTTP API**. The app and worker **never touch the Docker socket**. The laptop/dev baseline is **hardened Docker** (works on Docker Desktop); the **recommended production hardening runtime is gVisor (`runsc`)**, a drop-in swap requiring no code change.

### 1. Topology — a dedicated `sandbox-runner` service (worker calls it; the app never holds the Docker socket)

- A new **`sandbox-runner`** service in `docker-compose.yml` (pinned image, **no `:latest`**, [ADR-0005](0005-local-run-and-developer-workflow.md)) owns **all** container lifecycle. It is the **only** component with access to a container engine (the Docker socket, or a rootless/child daemon). It exposes a **small internal HTTP API** on the compose network — `POST /runs` (start a run), `GET /runs/{id}` (poll status/result), `POST /runs/{id}/cancel` — reachable **only** from the worker, never published to a host port.
- The **Celery worker** ([`tasks/`](../../backend/app/tasks/)) calls that API; **neither the FastAPI app nor the worker mounts `/var/run/docker.sock`.** Mounting the Docker socket into the app/worker is **equivalent to host root** and is explicitly forbidden — confining it to one small, audited service is the core of this design (it answers [#204](https://github.com/k-sandhu/lumen-copilot/issues/204) Q1: *dedicated runner, not the worker spawning sandboxes directly*).
- The runner is **stateless per run**: it accepts a run spec (code + staged input refs + limits + policy), launches **one ephemeral container**, streams/collects the result, then **destroys the container and its scratch**. It holds no tenant data at rest.
- **Boundary:** the owning application module is **`backend/app/sandbox/`** (run orchestration, the runner client, policy/quota enforcement, result mapping to domain types — it exposes **domain types only**, never the runner's or Docker's wire objects, per [ADR-0004](0004-architecture-boundaries-and-adapters.md)). The runner service itself is the out-of-process boundary. See the **boundary-table row proposed** at the end of this ADR.

### 2. Per-run container hardening (the enforced sandbox)

Every run launches an **ephemeral, single-use container** with, at minimum:

- **Network denied by default** (`--network none` unless an admin policy attaches a constrained egress path — §5). No route to the internet, to internal services, or to the metadata IP.
- **Read-only root filesystem** (`--read-only`); the writable working area is a **`tmpfs` scratch workdir** (size-capped) mounted at the run's CWD. No host paths are bind-mounted in.
- **Non-root user** (`--user` a dedicated unprivileged uid; `--security-opt no-new-privileges`).
- **All Linux capabilities dropped** (`--cap-drop ALL`); a restrictive **seccomp** profile; `--security-opt no-new-privileges` blocks setuid escalation.
- **Process cap** (`--pids-limit`) to stop fork-bombs.
- **CPU limit** (`--cpus`) and **memory limit** (`--memory`, so an over-allocating run is **OOM-killed** by the kernel rather than starving the host).
- **Wall-clock timeout** enforced by the runner: on expiry the container is **killed** (SIGKILL) regardless of internal state — a run cannot outlive its budget.
- **Output-size cap** on captured stdout/stderr and on collected output files; exceeding it truncates/fails the run rather than exhausting memory or storage.
- **No secrets or host env** passed in: the container receives only the run's explicit inputs and a minimal, curated env (§4).

**Runtime tiers.** The **baseline** is hardened Docker with the above flags — chosen because it runs on a laptop / Docker Desktop with no extra kernel features ([ADR-0005](0005-local-run-and-developer-workflow.md)). The **recommended production hardening runtime is gVisor (`runsc`)**: it interposes a user-space kernel between the workload and the host kernel, shrinking the shared-kernel attack surface that is container-per-run's main residual risk. gVisor is an OCI runtime, so enabling it is a **runner configuration change** (`--runtime=runsc`), **not** an application code change — the `sandbox/` module and the run/result contract are identical across runtimes. **Firecracker** (microVM, needs `/dev/kvm`) is recorded as a **future prod-only runtime** behind the same runner seam, deferred because it exceeds the laptop compose floor.

### 3. Base image — pinned Python + a curated scientific stack

- One **pinned base image** (`python:<pinned>-slim` + a **curated, version-pinned** scientific stack: **pandas, numpy, matplotlib** (headless `Agg` backend), **openpyxl**, and a small, reviewed set of common analysis libraries). Pinned by **digest**, **no `:latest`** ([ADR-0005](0005-local-run-and-developer-workflow.md)).
- **No arbitrary internet `pip install` by default.** With network denied (§2), a run cannot reach PyPI; the usable libraries are exactly what the base image ships. **Admin package allow/deny** ([#233](https://github.com/k-sandhu/lumen-copilot/issues/233)) governs any expansion — either by curating a larger pinned image or (if ever enabled) an admin-allowlisted, hash-pinned internal mirror. Arbitrary install-from-the-internet is **rejected by default**, mirroring the deny-by-default posture of [ADR-0009](0009-connector-framework-and-web-source.md).

**Amendment ([#503](https://github.com/k-sandhu/lumen-copilot/issues/503) / [#504](https://github.com/k-sandhu/lumen-copilot/issues/504), 2026-07-29) — the curated image above now EXISTS, as a separate artifact from the runner.** Until #503 no such image was ever built: `SANDBOX_IMAGE` defaulted to `lumen-sandbox-runner:0.2.0`, the **runner's own control-plane image** (python-slim + fastapi/docker/pydantic). Every run therefore executed with none of the stack this section specifies — "run Python" could not do data work at all — and tenant code ran in the image of the one service that holds the Docker socket. The execution image is now built by **`sandbox_exec/Dockerfile`** as **`lumen-sandbox-exec:<version>`** (compose service `sandbox-exec-image`, `sandbox` profile), from a **digest-pinned** `python:3.12.x-slim-bookworm` plus the exact, version-pinned closure in `sandbox_exec/requirements.txt` — **pandas, numpy, matplotlib (headless `Agg`), openpyxl** and their pinned transitive dependencies, installed `--no-deps` with a `pip check` that fails the build if that closure is incomplete. Two operational facts this section did not state, both established by failures rather than reasoning: the runner launches the image on the **host** daemon, so it must be **built there** before code execution is enabled (`docker compose --profile sandbox build sandbox-exec-image`; see the [runbook](../runbooks/sandbox-code-execution.md)); and because §2's `--cap-drop ALL` removes `CAP_DAC_OVERRIDE` from **uid 0 as well**, the session workspace must be world-writable rather than user-owned or the runner's first `mkdir` fails as root. Headless matplotlib additionally needs a **writable `MPLCONFIGDIR` with the font cache warmed at build time**, or the first plot in a network-isolated container falls back to a temp directory and prints a warning into the run's captured stderr. **Package policy is amended in ADR-0020 §3** (a distribution the image already ships is admitted without an install): what stays true here is that arbitrary install-from-the-internet is rejected by default, and that widening the usable set means **curating this image**, never opening sandbox egress.

**Amendment (PR #507 review, 2026-07-29) — "pinned by digest, no `:latest`" applies at LAUNCH time, not only at build time.** #503's amendment above satisfied that clause for the image's **base layer** (the `FROM` line, digest-pinned, guarded by a test that reads the Dockerfile as text). The reference the runner actually resolves per session is `SANDBOX_IMAGE`, and that value was **unvalidated**: `:latest`, a bare image name, or a remote ref were all accepted, so the strongest statement in this section described a layer while model-authored code executed in whatever a mutable tag pointed at. Two additions close the gap. (1) `Settings.sandbox_image` is validated at boot: an exact tag or a `name@sha256:<64 hex>` digest, never `:latest` and never a bare name, and the **digest form is required** whenever `SANDBOX_ENABLED=true` outside `ENVIRONMENT=local` (a tag is accepted in local dev as trust-on-first-build — the operator built it on that daemon). (2) The runner resolves the image on the **local** daemon before creating a container and refuses with `503` when it is absent, because docker-py's `containers.run` otherwise catches `ImageNotFound` and **pulls** — which would have made an unverified image a registry fetch performed by the only Docker-socket holder, executed as contained root. Provenance is therefore an explicit operator step (build, or `docker pull` the digest onto the sandbox host); the runner never fetches.

**Amendment (PR #507 review, 2026-07-30) — the hash-pinned mirror this section conditions installs on does NOT exist; the allow-list path fetches UNVERIFIED wheels from the public PyPI.** This section permits install-based expansion only via "an admin-allowlisted, **hash-pinned internal mirror**". ADR-0020 §3 built the install path and the #504 amendment above widened admission, and neither disclosed that the mirror was never implemented. What the runner runs is `pip download --only-binary=:all: --dest <tmp> -- <packages>` against the **default public index**: no `--require-hashes`, no `--index-url` pin, no lockfile. So an `allowed_packages` grant trusts whatever PyPI serves at the moment of the fetch, and a compromised or typosquatted release of an allow-listed distribution is installed and imported as root inside the execution container — contained by §2 (no network, no mounts, no socket, no capabilities, gVisor outside local dev), but root. Installation precedes input staging (ADR-0020 §3), so the installer never sees tenant bytes; the installed code does, on every later execution in that session. Four things bound it today and all of them are deliberate: a tenant `allowed_packages` list that is **empty by default**, so a deploy that grants nothing fetches nothing; the pre-installed image manifest, which needs no index at all; `--only-binary=:all:`, so no `setup.py` executes in the socket-holding runner during resolution; and withholding the runner's outbound network entirely, which is the intended locked-down posture and leaves the image manifest as the whole usable set. Two smaller hardenings landed with this amendment: an end-of-options `--` separator before the package list in both pip argv lists, and an asserted PEP-503 name pattern on every parsed requirement, so a "package" can never be read as an option such as `--index-url=`. **A hash-pinned internal mirror remains the correct fix and is not implemented** — it is filed as a follow-up, and until it exists this section's clause is aspirational rather than descriptive. Deny scope is also stated plainly, because the runbook had dropped it: deny is applied to **directly requested** names only (ADR-0020 §3), while the wheelhouse is resolved with dependencies, so a denied distribution can still arrive as a transitive dependency of an allowed one and the audit row records only the top-level name.

### 4. Execution model — a Celery task, off the request path (run/result shapes for the dependents)

Runs execute **off the request path as a Celery job**, exactly like ingestion ([backend/AGENTS.md](../../backend/AGENTS.md); [ADR-0009](0009-connector-framework-and-web-source.md) §4 established this shape). The HTTP endpoint validates + enqueues; a worker task calls the runner; progress/results stream back over the existing WebSocket transport ([`realtime/`](../../backend/app/realtime/)). These sketches are **guidance for the dependent contract/impl issues** ([#229](https://github.com/k-sandhu/lumen-copilot/issues/229) contract, [#230](https://github.com/k-sandhu/lumen-copilot/issues/230) execution service), to be frozen in `contracts/` there:

**`code_runs` table** (tenant/owner-scoped; the run record — refined by the migration in [#230](https://github.com/k-sandhu/lumen-copilot/issues/230); ordinary tenant-scoped `tenant_id`/RLS per [spec 0004](../specs/0004-security-and-domain-invariants.md) §2.1):

```
code_runs(
  id, tenant_id, owner_id,
  status,                 -- queued | running | succeeded | failed | timeout | killed | denied
  code,                   -- the exact source executed (inspectable, E15-7/E6-5)
  input_refs,             -- jsonb: artifact/document refs staged read-only into the run
  limits,                 -- jsonb: cpu, mem, pids, wall_clock_s, output_bytes_cap
  policy,                 -- jsonb: egress allowlist (default none), package policy, runtime
  image_digest,           -- pinned base-image digest actually used (reproducibility, E3-7)
  exit_code, stdout, stderr,   -- captured, output-size-capped
  duration_ms, resource_usage, -- jsonb: peak mem, cpu time, etc.
  started_at, finished_at, created_at,
  trace_id                -- links the run to the parent agent trace (E15-7 "runs link to the parent trace")
)
```

**Run request** (worker → runner, `POST /runs`): `{ run_id, tenant_id, code, input_refs[] (staged read-only), limits{cpu, mem, pids, wall_clock_s, output_bytes_cap}, policy{egress: none|allowlist[...], packages, runtime} }`.

**Run result** (runner → worker / persisted): `{ status, exit_code, stdout, stderr, output_files[] (collected from the designated output dir), duration_ms, resource_usage, image_digest }`.

- **Inputs** (e.g. an uploaded dataset) are staged **read-only** into the run from the artifact/object store; the code cannot mutate the source.
- **Output files** the code writes to the designated output dir are collected and persisted as **artifacts via CC-B** ([#208](https://github.com/k-sandhu/lumen-copilot/issues/208)), tenant-scoped like any other artifact. The run record links them.
- **Determinism / reproducibility (E3-7):** each run records **code + input refs + the pinned `image_digest`** actually used, plus stdout/stderr/exit/duration/resource-usage — enough to **re-run** it and to inspect it.
- **Registry:** `run_python` registers as a **tool** in the agent tool platform (CC-A, [#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) at the **highest risk tier, admin-gated** ([#231](https://github.com/k-sandhu/lumen-copilot/issues/231)).
- **Audit (INV-6):** every run request/complete/deny emits an **audit event** through the one sink ([spec 0004](../specs/0004-security-and-domain-invariants.md) §2.4). Under the read-before-write tiers ([spec 0004](../specs/0004-security-and-domain-invariants.md) §2.5), *executing code that only computes and writes artifacts within the tenant* is an internal action; **egress or any external write stays T2+** (approval-gated, INV-7) — deny-by-default egress keeps a run T0/T1 unless an admin opts a tenant into a constrained allowlist.

### 5. Isolation guarantees — each with its mandatory negative test

The sandbox exists to make these guarantees **provable**. Each is enforced as above and ships a **negative test** ([spec 0004](../specs/0004-security-and-domain-invariants.md) §3, [AGENTS.md](../../AGENTS.md) §9 test-first). **A bypass is a blocking defect** — the same bar as the SSRF chokepoint in [ADR-0009](0009-connector-framework-and-web-source.md) and the retrieval permission filter in [ADR-0010](0010-dedicated-text-search-engine.md).

| # | Guarantee | Enforced by | Negative test (must fail closed) |
|---|---|---|---|
| **G1 Host filesystem unreachable** | read-only rootfs; tmpfs-only scratch; **no** host bind-mounts | run code that lists/reads/writes host paths (`/`, `/etc`, `/var/run/docker.sock`, the app source) → **all denied / not present**; writes land only in tmpfs scratch and vanish with the container |
| **G2 Egress → internet blocked** | `--network none` by default | run code that connects to an external host/DNS → **fails (no network)**; only an admin allowlist (§ policy) may open a specific target |
| **G3 Egress → internal services blocked** | no network; runner not on the app data network by default | run code that dials **Postgres / Redis / MinIO / OpenSearch** by service name or IP → **refused/unreachable** unless explicitly admin-allowed |
| **G4 Metadata IP blocked** | no network; (with any allowlist) `169.254.169.254` never allowlistable | run code that GETs `http://169.254.169.254/…` → **unreachable**, even when an egress allowlist is configured |
| **G5 No secret / env leakage** | minimal curated env; app secrets/DB creds never injected | run code that dumps `os.environ` / reads mounted secret paths → **no app secrets, DB URL, tokens, or object-store creds present** |
| **G6 CPU / memory / pids / wall-clock enforced with kill** | `--cpus`, `--memory` (OOM-kill), `--pids-limit`, runner timeout SIGKILL | a busy-loop → **wall-clock-killed** (`status=timeout`); an over-allocation → **OOM-killed** (`status=killed`); a fork-bomb → **pids-capped** |
| **G7 Output-size cap** | capped stdout/stderr + output-file collection | code that prints/writes beyond the cap → **truncated/failed**, host memory & storage bounded (no OOM of the runner) |
| **G8 Fresh sandbox per run (no residue)** | new ephemeral container + fresh tmpfs each run; container destroyed after | run A writes scratch/leaves state → run B (esp. **another tenant**) **cannot see** any of it; each run starts from the pinned image only (INV-1 tenancy) |

Testing note: G2/G3/G4 are validated with a policy fixture that simulates the deny-by-default and (separately) the admin-allowlist path; G4 asserts the metadata IP is **never** reachable even under an allowlist. These run against the compose `sandbox-runner`.

### 6. Quotas & kill-switch — default OFF

- **Per-tenant concurrency cap** (max simultaneous runs) and a **per-tenant daily-runtime cap** (aggregate wall-clock/day); exceeding either **queues or rejects** further runs (audited).
- **Admin per-tenant enable — default OFF.** Code execution is **disabled for every tenant until an admin explicitly enables it** for that tenant ([#233](https://github.com/k-sandhu/lumen-copilot/issues/233)); an admin can disable it at any time (kill-switch). This matches the highest-risk-tier, admin-gated stance of the epic and CC-A ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)).

## Consequences

- **Upside:** the highest-risk capability ships behind a **single, small, auditable trust boundary** (the `sandbox-runner` service) rather than sprinkling Docker-socket access through the app; the isolation guarantees are **enumerated and negative-tested**, so "the sandbox contains the code" is provable, not asserted. It comes up with `docker compose up` on a laptop (hardened Docker) and **hardens to gVisor in production by config alone** — no code churn. Runs are reproducible (pinned image digest + recorded code/inputs) and inspectable (code + stdout/stderr + timing linked to the trace).
- **Cost / risk:**
  - **Container-per-run shares the host kernel** on the Docker baseline — the residual escape surface. Mitigated by cap-drop / seccomp / non-root / read-only / no-network, and **closed further by gVisor in production** (recorded as the recommended hardening runtime). Firecracker remains the escalation path if microVM isolation is ever required.
  - A **new privileged-ish service** (`sandbox-runner`, the only Docker-socket holder) is added to the stack; keeping the socket **out of** the app/worker is the deliberate trade — the socket lives in exactly one place, behind an internal API the worker calls.
  - **Per-run container churn** costs startup latency and image storage; acceptable because runs are already **off the request path** (Celery), and the base image is pinned/cached.
  - The **isolation guarantees must be continuously re-proven** with the G1–G8 negative tests — the main correctness/security risk; a regression there is a blocking defect.
  - Raises the local-run floor slightly (an extra service + the base image); mitigated by the runner being idle until a run is enqueued and the whole thing being **default OFF** per tenant.
- **Delivery** (per [ADR-0008](0008-conflict-free-parallel-delivery.md), serialized seam → parallel build): this ADR unblocks **[#229](https://github.com/k-sandhu/lumen-copilot/issues/229)** (run wire / contract freeze), **[#230](https://github.com/k-sandhu/lumen-copilot/issues/230)** (sandbox execution service + `code_runs` migration + `sandbox-runner` compose service), then **[#231](https://github.com/k-sandhu/lumen-copilot/issues/231)** (`run_python` tool, needs CC-A [#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) ‖ **[#232](https://github.com/k-sandhu/lumen-copilot/issues/232)** (inspector UI), and **[#233](https://github.com/k-sandhu/lumen-copilot/issues/233)** (admin sandbox policy). Output-file capture depends on CC-B artifacts ([#208](https://github.com/k-sandhu/lumen-copilot/issues/208)).

## Boundary-table row to propose (needs human approval — [AGENTS.md](../../AGENTS.md) §6)

This ADR adds a **new owning module + out-of-process service**. Per [AGENTS.md](../../AGENTS.md) §6/§7.6 ("a new external system ⇒ a new module **and** a new row here, in the *same* change") the following row must be added to the [AGENTS.md](../../AGENTS.md) §6 boundary table / [ADR-0004](0004-architecture-boundaries-and-adapters.md). **Agents may not edit `AGENTS.md`** ([AGENTS.md](../../AGENTS.md) §5) — this row is **proposed here and requires human approval** before it lands:

| Concern | Single owning module |
|---|---|
| Sandboxed code execution | `backend/app/sandbox/` (+ the `sandbox-runner` compose service) |

`backend/app/sandbox/` exposes **domain types only** and is the **only** caller of the `sandbox-runner` internal API; the runner service is the **only** holder of container-engine / Docker-socket access. No other module orchestrates code execution or talks to the runner.

## Provenance

- **Decided by:** human sponsor (mechanism delegated to this spike within the epic's decided constraints) + Claude Opus 4.8, recorded 2026-07-02.
- **Inputs:** [#204](https://github.com/k-sandhu/lumen-copilot/issues/204) (spike brief + options table), epic [#200](https://github.com/k-sandhu/lumen-copilot/issues/200); [ADR-0003](0003-application-stack.md)/[ADR-0005](0005-local-run-and-developer-workflow.md) (OSS-only, one-command compose), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (boundaries), [ADR-0009](0009-connector-framework-and-web-source.md) (deny-by-default egress precedent), [spec 0004](../specs/0004-security-and-domain-invariants.md) (tenancy/permission/audit/read-before-write invariants).
- **Traceability:** closes SPIKE-3 ([#204](https://github.com/k-sandhu/lumen-copilot/issues/204)); unblocks [#229](https://github.com/k-sandhu/lumen-copilot/issues/229), [#230](https://github.com/k-sandhu/lumen-copilot/issues/230), [#231](https://github.com/k-sandhu/lumen-copilot/issues/231), [#232](https://github.com/k-sandhu/lumen-copilot/issues/232), [#233](https://github.com/k-sandhu/lumen-copilot/issues/233). Proposes the `backend/app/sandbox/` boundary row above (needs human approval; [AGENTS.md](../../AGENTS.md) §6 not edited here). Defaults no open decision in [spec 0001](../specs/0001-open-decisions.md).
