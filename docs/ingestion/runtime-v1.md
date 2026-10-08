# Bounded computation runtime v1 (#666)

One reusable Runtime per Celery child owns an explicitly sized rayon pool; no
global rayon pool is used. Set worker process concurrency to one initially,
prefetch to one (already configured), pool width to two and admitted documents
to one. Later tune process count times pool width against allocated cores/RAM.
The foundation exposes the executor but does not dispatch production formats;
#687 owns cutover. Python keeps Celery leases, retries, I/O, audit and readiness.

Each document context owns a cancellation token, monotonic deadline and atomic
memory/work/output counters. Reservations charge BEFORE allocation and release
on drop; outputs retain their reservations through serialization. Work units and
output character totals are monotonic. A parser must use the context for every
expanded/intermediate allocation and checkpoint every bounded unit. Native
copy/scan work checks every 1024 Unicode scalars. Input/JSON and serialization
are bounded independently; the facade reserves worst-case JSON escaping space.
No successful-looking truncated result is returned on any budget/cancel failure.

Admission is fail-fast when the configured in-flight cap is occupied, so a
second call cannot deadlock a pool thread waiting for another document's permit.
Python may retry/requeue the typed budget error. Batch execution proceeds in
bounded waves, preserves order and retains individual failures for Rust callers.
The Python per-document API raises a typed exception for every failed operation.
A panic in an individual unit is contained and translated; the pool remains usable.

Memory counters bound accounted Rust allocations, not process RSS, Python
objects or an uninstrumented dependency. Non-cooperative native engines still
need isolated processes and hard supervisors before adoption (ADR-0026).
Cooperative cancellation targets <=100 ms with <=10 ms/bounded-unit checkpoints;
handshake tests establish cancellation at the next checkpoint without a timing
race. Benchmark reports distinguish executor scaling from format extraction,
whose candidate arms remain unavailable until parsers land.

Python feeds large documents as bounded units through the executor; the core
also accepts bounded iterators in sequential windows without retaining the
whole input. Python performs all underlying stream/network/storage reads.

Config lives in `core/config.py`: NATIVE_INGESTION_THREADS,
NATIVE_INGESTION_MAX_DOCUMENTS, NATIVE_INGESTION_MAX_MEMORY_BYTES,
NATIVE_INGESTION_MAX_INPUT_BYTES, NATIVE_INGESTION_MAX_OUTPUT_CHARS,
NATIVE_INGESTION_MAX_WORK_UNITS and NATIVE_INGESTION_TIMEOUT_MS. Pass these
through RuntimeBudget and NativeExecutor; no configuration enables a parser.
The optional compose override caps process concurrency without changing the
general-worker default. Streaming sessions retain source input/work/output
counters and the same deadline across windows.
