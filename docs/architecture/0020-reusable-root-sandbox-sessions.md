# 20. Reusable root-capable Python sandbox sessions

- **Status:** Accepted *(human sponsor direction, 2026-07-18; issue [#457](https://github.com/k-sandhu/lumen-copilot/issues/457))*
- **Date:** 2026-07-18
- **Supersedes in part:** [ADR-0013](0013-code-execution-sandbox.md) — replaces fresh-container-per-run, non-root, read-only-rootfs, per-run resource caps, and automatic wall-clock expiry for interactive `run_python` execution. The dedicated runner boundary, tenant/owner scoping, audit, artifact capture, no host mounts/socket/secrets, and production gVisor posture remain in force.
- **Builds on:** [ADR-0004](0004-architecture-boundaries-and-adapters.md), [ADR-0006](0006-contract-first-parallel-implementation.md), [spec 0004](../specs/0004-security-and-domain-invariants.md), and story E15-7.

## Context

ADR-0013 deliberately chose a fresh, tightly capped, non-root container for every
code run. That model contains residue well, but it prevents an assistant from building
an analysis environment over multiple turns: a package installed in one run, a file
created for a later step, or a cached intermediate result disappears immediately.

Issue #457 changes the product requirement: one isolated Python environment belongs to
one chat session and is reused until the user resets it, closes it, or deletes the chat.
The model runs as root **inside that isolated container** so approved packages can be
installed into the mutable environment. This intentionally trades the original
resource-abuse posture for interactive flexibility: v1 has no automatic execution or
idle timeout and no per-run CPU, memory, PID, output, or daily-runtime limit.

Root plus direct network access would let model-authored code bypass domain allowlists,
scan internal services, or exfiltrate staged tenant data. Root therefore does not imply
network authority. Package acquisition is a separate, governed operation: the runner
downloads only package specs admitted by the tenant policy, copies the wheels into the
offline sandbox, and performs the install there as root.

## Decision

### 1. Identity and lifecycle

- A durable `sandbox_sessions` row is keyed by `(tenant_id, chat_session_id)` and also
  carries `owner_id`, `generation`, lifecycle status, image digest, and timestamps.
- Its UUID is the runner session key. The runner labels the container with the opaque
  session UUID and generation; it never receives tenant/user IDs or credentials.
- Repeated `run_python` calls in the same chat use the same active generation. Different
  chats, users, and tenants never share a row or container.
- `POST /chat/sessions/{id}/sandbox/reset` destroys the current container, increments
  the generation, and creates a clean replacement. `DELETE .../sandbox` closes it.
  Deleting the parent chat closes the sandbox before the database row is removed.
- If replacement creation fails after teardown, the advanced generation is committed
  as `error` rather than falsely restoring the old generation. The next reset/use
  advances again and creates a clean recovery generation.
- There is no automatic idle expiry. A runner restart rediscovers live containers by
  labels. Explicit reset/close/cancel is the only normal teardown.
- Executions within one session serialize in the runner. Different sessions may run in
  parallel. Database compare-and-set transitions make duplicate task delivery and a
  cancel/completion race idempotent: the first terminal state wins. Lifecycle
  generation changes are optimistic and reject a stale concurrent reset/close.

### 2. Root is contained, not host authority

The session container is writable and executes as UID 0, but it receives:

- no host bind mounts;
- no Docker/container-engine socket;
- no application source, database/object-store credentials, or host environment;
- no Linux capabilities (`cap-drop ALL`) and `no-new-privileges`;
- no network interface for model-authored execution;
- only explicit read-only input bytes staged through the runner API.

The Docker baseline remains suitable only for local development. When sandbox execution
is enabled outside `ENVIRONMENT=local`, configuration must require gVisor (`runsc`). A
future microVM runner may implement the same session protocol without changing callers.

### 3. Governed package installation

`run_python` gains an optional `packages` list. Package requirements are canonicalised
and their directly requested distribution names are checked against the effective
tenant `allowed_packages`/`denied_packages` policy; `*` is the explicit allow-all grant
and deny entries always win for a direct request. Dependencies resolved from an admitted
top-level requirement are part of that grant. A rejected package denies the run before
a container command executes.

For admitted requirements, the runner downloads binary distributions into its own
temporary directory, copies them into the session, and runs `pip install --no-index`
inside the offline container as root. Inputs are staged **after** installation. Thus a
package installer never receives tenant data and model-authored code never receives a
network route. Installed packages persist for the session generation.

### 4. Execution and persistence

- The runtime container starts once and remains idle between executions.
- Each execution writes its code and output directory under a run-specific path in the
  session workspace. `LUMEN_OUTPUT_DIR` identifies files to collect as artifacts.
- The general `/workspace` filesystem persists so later turns can reuse files and
  installed packages. Reset/close destroys it completely.
- `code_runs` records `sandbox_session_id` and `sandbox_generation` alongside the code,
  image digest, stdout/stderr, exit, duration, artifacts, and trace.
  The run's opaque sandbox UUID is historical data rather than a foreign key, so it
  survives deletion of the chat-scoped lifecycle row for audit/reconstruction.
- There is no automatic runtime/resource/output limit. `duration_ms` and best-effort
  usage remain observational. The compatibility statuses `timeout` and `killed` remain
  readable for historical rows and explicit cancellation/runtime failures.

### 5. Runner boundary and cancellation

The in-repo `sandbox_runner/` service is the only Docker-socket holder. Its internal API
supports ensure/inspect, execute, reset/close, and cancel. The application continues to
depend only on domain `SandboxRunner` types in `backend/app/sandbox/`.

Cancel destroys the run's matching container generation because arbitrary root processes
cannot be safely selectively terminated. If that generation is still current, the
application increments it so the next execution starts clean. A late cancellation of an
older run never destroys or advances a newer replacement. This is an explicit
user/operator action, not a timeout.

### 6. Audit and negative tests

Additive audit events record `sandbox_session.created`, `.reset`, `.closed`, and
`code_run.cancelled`. Required negative tests prove:

1. another chat/user/tenant cannot observe or reset a session;
2. the runtime has UID 0 but no host mounts, socket, app secrets, or network;
3. denied packages never reach the runner and admitted installs happen before inputs;
4. reset removes files/packages and advances the generation;
5. concurrent executions in one session serialize;
6. chat deletion closes the runner session;
7. non-local sandbox enablement with `runc` fails configuration validation.

## Consequences

- Multi-turn analysis becomes materially more capable: installed dependencies, cached
  datasets, generated intermediate files, and local indexes survive between tool calls.
- There is no automatic protection against infinite loops, fork bombs, disk growth, or
  resource monopolisation in this first version. Explicit cancel/reset is the recovery
  path. This is a deliberate sponsor decision and the largest residual operational risk.
- Root increases the consequence of a container-runtime escape. Mandatory gVisor outside
  local development and the no-mount/no-socket/no-network boundary are release gates.
- Long-lived mutable environments are less reproducible than immutable per-run images.
  Recording the session generation, installed requirements, code, and image digest is
  therefore mandatory.
- Package supply-chain risk remains even without sandbox egress. Admin package policy,
  binary-only downloads, pinned requirements where practical, and the audit trail are
  the initial controls; hash-locked organisation mirrors are a future hardening option.
