# 29. Bounded sandbox sessions by default

- **Status:** Proposed *(owner decision required in review; issue [#589](https://github.com/k-sandhu/lumen-copilot/issues/589))*
- **Date:** 2026-10-29
- **Amends:** [ADR-0020](0020-reusable-root-sandbox-sessions.md) — reverses its unbounded-by-default resource posture only. Reusable sessions, contained root, network-none, governed package installation, and the runner boundary are unchanged.
- **Builds on:** [ADR-0013](0013-code-execution-sandbox.md), [ADR-0004](0004-architecture-boundaries-and-adapters.md), [spec 0004](../specs/0004-security-and-domain-invariants.md).

## Context

ADR-0020 deliberately shipped reusable sandbox sessions with no automatic
CPU, memory, PID, wall-clock, scratch, concurrency, or daily-runtime limit,
naming that the design's largest residual operational risk. Issue #589 is
that risk arriving as a defect report: the admin governance surface could
not show or prove any effective cap, tenant policy values that looked like
limits were read by no enforcement path, and one tenant's run could
monopolise the host every tenant shares. Output collection was the one
bounded quantity (#519/#554), and even it was described in the UI as
unlimited.

## Decision

1. **Bounded by default.** Deployment hard ceilings exist for every named
   quantity and are enforced unless a deploy explicitly opts out
   (`SANDBOX_SESSION_LIMITS_ENABLED=false`), which restores ADR-0020's
   container posture for emergency compatibility recovery. Deadlines, output caps
   and tenant quotas still apply. This switch is visible in the admin console;
   it is not a supported way to bypass accounting.

   | Bound | Default ceiling | Enforced where |
   |---|---|---|
   | CPU | `SANDBOX_CPUS` (1.0) | runner `--cpus` (`nano_cpus`) |
   | Memory | `SANDBOX_MEMORY_BYTES` (512 MiB) | runner `--memory` |
   | PIDs | `SANDBOX_PIDS_LIMIT` (128) | runner `--pids-limit` |
   | Wall clock (per execution) | `SANDBOX_WALL_CLOCK_SECONDS` (30) | runner destroys the container at the deadline |
   | Scratch (writable session workspace) | `SANDBOX_SCRATCH_BYTES` (256 MiB) | tmpfs-backed volume on `/workspace` |
   | Output collection | `SANDBOX_OUTPUT_BYTES_CAP` (32 MiB) | runner collection budget (unchanged) |
   | Per-tenant concurrency | `SANDBOX_MAX_CONCURRENT_PER_TENANT` (2) | Redis lease |
   | Per-tenant daily runtime | `SANDBOX_DAILY_RUNTIME_SECONDS_PER_TENANT` (3600) | Redis daily counter |

2. **Tenant overrides may only narrow.** The per-tenant policy fields
   (`max_runtime_s`, `max_memory_mb`, `daily_runtime_cap_s`,
   `max_concurrency`) are clamped by `min()` against the deployment
   ceilings at resolution time, and a write that asks to exceed a ceiling
   is rejected **422** instead of being silently clamped, so an admin is
   told no rather than shown a different value than they submitted.
   Effective values carry their source (`tenant_override` vs
   `deployment_default`) through the admin API to the UI. CPU, PID,
   scratch, and output bounds have no tenant field and are
   deployment-wide.

3. **Atomic, tenant-scoped accounting.** Admission takes a Redis
   concurrency lease (sorted set scored by expiry; one Lua evaluation
   prunes, counts, and admits) and reserves the run's wall-clock budget
   against a UTC-day counter, settled to elapsed seconds (rounded up and bounded
   by the reservation) on completion. Settlement uses the admission date even
   across midnight. A lost worker leaves a conservative full reservation until
   that day expires. Duplicate delivery cannot reacquire an active run lease.
   A live producer renews its fenced lease, including during cancellation
   attempts and retry backoff. Renewal only extends an unexpired lease owned
   by that producer; it cannot resurrect an expired lease or release a newer
   owner's lease. Renewal failure requests termination and never proves it: it may cancel only the in-flight remote execution call, never the producer task as a whole. Before that call starts the run is refused fail-closed; after it returns the run finalises normally. An accounting-loss kill is recorded `killed` with `termination_reason`/`reason_code` `sandbox_accounting_unavailable` and a `code_run.finished` error audit — distinct from a user Stop, which alone records `cancelled` with an allowed `code_run.cancelled` audit. The first established cause wins that classification: an accounting loss latched before a Stop stays `sandbox_accounting_unavailable` even if the Stop interrupts cleanup (the chat turn still reflects the Stop), and a Stop observed first stays `cancelled` even if a later renewal tick fails.
   Cancellation retries the same execution identity with exponential backoff (1s doubling to a 60s ceiling); only confirmed runner
   termination permits `killed`, an allowed cancellation audit, and one
   settlement/release. Lease, heartbeat and reservation are held by one ownership object from admission until confirmed termination (settle/release once, then renewal stops) or genuine producer death (renewal stops, no refund, expiry recovers); a producer cancellation at any cleanup await while unconfirmed hands that same object to whoever continues cleanup, so nothing else can stop renewal on a live path. The first failed or interrupted attempt emits an error outcome (later attempts are logged, without an audit row each; the latch is shared by the service loop and the chat confirmed-kill callback, so one execution gets one error row)
   with a safe reason code, never raw diagnostics. Pending cleanup keeps the
   reservation and lease; producer shutdown stops renewal without refunding.
   Its TTL (wall clock + grace) still reaps holders that die mid-run. If Redis cannot
   answer, admission **fails closed** with
   `sandbox_accounting_unavailable` — unlike the fetch rate limiter,
   these counters are the enforcement mechanism, not an availability
   guard.

4. **Runner enforcement details.** Bounds are creation-time container
   configuration; a reused container whose bound fingerprint differs
   from the requested bounds is destroyed and recreated (the same
   treatment a runtime mismatch already gets). Scratch is a sized tmpfs
   on `/workspace` — chosen over overlay storage quota because it does
   not depend on host filesystem quota support. It is mounted as an
   anonymous local-driver **volume** with tmpfs options (`size`, `nosuid`,
   `nodev`), never a `tmpfs` mount: Docker's archive API, which stages code
   and inputs and collects outputs, cannot see tmpfs mounts, so a tmpfs mount
   failed every bounded execution (#838). The volume is removed with its
   container. The base filesystem becomes
   read-only when scratch bounds are enabled so code cannot bypass the quota
   via another directory. Home, temp files and installed packages move into
   that same workspace; contained root can still mutate that environment.
   Packages install into a run-specific staging target, then merge into
   `/workspace/.packages`, on `PYTHONPATH` in both container postures. Replacing
   a distribution removes its previous RECORD-owned files and metadata before
   merging, preserving namespace files contributed by other distributions.
   The imported version agrees with the recorded wheel resolution. This is a necessary
   amendment to ADR-0020's writable-rootfs choice, not a new sandbox technology.
   Tmpfs pages count toward the memory limit; swap is disabled. The scratch
   size is a maximum, not a guarantee of that much free RAM.
   An absolute request deadline also prevents code from starting after a long
   queue wait. At the wall-clock deadline the runner destroys the container (the
   generation-wide teardown cancel already uses) and reports
   `timeout` / `sandbox_limit_wall_clock`. Docker `OOMKilled` or an allocation failure is classified
   `sandbox_limit_memory`; a scratch-full
   workspace surfaces as `sandbox_limit_scratch`; truncated output
   collection as `sandbox_limit_output`. PID exhaustion reports
   `sandbox_limit_pids` from cgroup v2 events or allocation failures. All
   limit outcomes destroy the container; output overages report `failed`
   and retain only complete collected files. Runtimes that omit cgroup
   events fall back to Docker OOM state and process error text; attribution
   is best-effort when code catches or imitates an error. The cgroup/tmpfs
   enforcement remains in force independently of attribution. On creation
   and reuse the runner verifies actual HostConfig bounds, not just labels.
   Before collecting outputs or returning admission, the runner kills all
   execution-created processes in the container's PID namespace, including
   detached/reparented descendants with closed descriptors. The pre-execution
   idle processes are identified by PID and start time; session files remain.
   Cleanup runs through isolated Python startup and failure destroys the
   container. The deadline remains armed through this cleanup and collection;
   elapsed accounting includes these phases. Runner-side wheel acquisition uses
   the same absolute deadline, kills its process group and reaps the downloader
   before returning. Docker I/O, archive streams and collection-permit waits
   consume that same remaining budget rather than starting independent timers.

5. **Provenance.** Every admitted run persists its resolved
   `effective_limits` (values + sources) at the `running` transition and
   its `termination_reason` at the terminal transition
   (`code_runs` columns, migration `0052_sandbox_limits`). Denials are
   audited with their typed reason as before; policy writes remain
   audited.

## Consequences

- A `while True` run now ends at its wall clock (default 30s, tenant-
  narrowable) instead of running until somebody cancels it. Long
  analyses need deployment ceilings raised deliberately; tenant values can
  only increase within those ceilings — that friction is the point, and the denominator is visible
  in the admin console.
- Container destruction at the deadline discards the session
  generation's filesystem, exactly like cancel. The next run in the
  chat starts from a fresh container for the same generation.
- Execution now depends on Redis at admission time. A Redis outage
  refuses new runs (typed, audited) instead of admitting them
  unaccounted. A run still executing remotely when its lease renewal fails is killed with `sandbox_accounting_unavailable`; a run whose execution already returned finalises normally.
- Legacy packages that require writes outside the workspace must be adapted;
  the read-only base is intentional so scratch cannot be bypassed.
- Reversing this ADR is the config default
  (`SANDBOX_SESSION_LIMITS_ENABLED`) plus this ADR's status; the
  accounting and persistence remain correct under either posture.

## Owner decision required

This ADR reverses a deliberate sponsor decision in ADR-0020. It stays
**Proposed** until the owner approves it in review of the #589 change;
the implementing PR carries the same gate ("do not merge until the
owner has reviewed").
