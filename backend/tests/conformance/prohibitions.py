"""Package-level structural rules: enrollment + the §4 execution-context prohibitions.

> *"Connector code **never** reads the vault, the DB, or mutable module state;
> the conformance kit pins these prohibitions (a connector that imports the
> secrets service or a repository fails conformance)."* — ADR-0019 §4

Why structural rather than behavioural: the prohibition is about what a
connector *can* reach, not what a particular test happens to exercise. A
connector that resolves its own credential works fine in every green-path test
and still breaks the model — the framework's guarantee is that a raw token
never enters connector code, that persistence decisions belong to the
framework's per-page transaction (ADR-0019 §3), and that a connector is
re-entrant because it holds nothing between runs. So the check is a static AST
scan of ``app/connectors/<name>/`` — every module in the package, including
function-local and **relative** imports (the two shapes a smuggled import
actually takes).

## Enrollment (:func:`check_registry_enrollment`)

The registry *skips* a ``CONNECTOR`` that does not satisfy the runtime protocol
(``registry.py`` ``continue``\\ s on a failed ``isinstance``). So a newly dropped
connector missing, say, ``health`` is simply **absent** from
``registered_types()`` — and a suite parametrized over the registry would pass
green while the new connector silently does not exist. Enrollment is therefore
checked from the **filesystem**: every connector-shaped subpackage must expose
``CONNECTOR`` *and* appear in the registry under its own directory name.

## The prohibitions (:func:`check_execution_context_prohibitions`)

**P1 — fail-closed import allowlist** (:data:`ALLOWED_FRAMEWORK_IMPORTS`): own
package modules, pure ``app.domain`` types, the four exact shared modules used
by web/Drive, and the sealed config accessor below. All other ``app`` imports
fail, including API/service wrappers that could reexport Settings or a session.
In-repo dotted imports require an alias or a from-import; unaliased imports
bind the broader root ``app`` namespace. Root-qualified ``app.*`` references
are refused too, regardless of which import introduced the namespace.
The existing :data:`FORBIDDEN_IMPORTS` add specific remediation messages for
the vault, DB and object store. Deliberately **not** banned: ``sqlalchemy`` itself. The ADR
prohibits touching *Lumen's* database, not the existence of SQL — a future
connector whose external source is a SQL warehouse legitimately imports a SQL
client, and the SDK has no business prohibiting a vendor boundary.

**P2 — the settings seam is sealed to one shape** (:func:`_settings_seam`). P1
alone does not survive contact: ::

    from sqlalchemy.ext.asyncio import create_async_engine
    create_async_engine(get_settings().database_url)      # never imports app.db

opens a connection straight into Lumen's database while importing nothing
forbidden. The obvious repair — work out whether a given read resolves to a
settings object — is a dataflow analysis, and hand-rolling one in a test kit
does not converge: conditionals, walrus, tuple unpacking, ``d = dict(d)`` and
cross-function laundering each need another case, and every added case risks a
*false positive* on a connector's own config, which is the more expensive
failure of the two.

So the shape is pinned instead, which AST can actually prove. A connector may
touch settings in exactly one form — ``get_settings().<field>`` — with the
accessor never aliased, stored, passed, or transformed, and the read
terminating at the field. A connector therefore never holds a settings object,
or a bound method retaining it: the terminal attribute must be an explicitly
allowed field. And because the rule only inspects expressions rooted at
``get_settings``, a connector's own ``connector_config.database_url`` is
invisible to it — the external-SQL allowance survives by construction.

Deployment config stays readable, because ADR-0019 §4/§5 explicitly sanctions
it (``oauth_spec()`` reads the platform's OAuth client registration; ``web``
reads its User-Agent). Lumen's *infrastructure* fields
(:data:`FORBIDDEN_SETTINGS`) are refused even through the one legal shape — and
that blacklist is **enforced-complete against the live ``Settings`` model**: a
completeness test asserts every field is classified forbidden-infra
(:data:`FORBIDDEN_SETTINGS`) or allowed-config
(:data:`ALLOWED_DEPLOYMENT_CONFIG`), so a newly added infra field cannot slip
through unlisted. Static relative and parent-module imports are checked too;
dynamic/reflective access stays review-caught under the first-party trust model.

**P3 — retained values must match IMM.** Guide §3 records the strict whitelist
grammar verbatim. Module/class bindings and every function/lambda default are
checked by that grammar, including referenced package declarations. There is no
expression-result, factory or iterable inference, and no constructor/handle
exemption. Only simple name assignments or
equal-arity unstarred tuple displays bind retained values. All other binding
forms fail closed. The earlier attribute/decorator/global/nonlocal and lexical
mutation checks remain secondary protection; borrowing cannot rescue a value
whose construction is forbidden. Class definitions may declare call-local field
factories for call-local instances. Retained constructor calls are forbidden.
Static dunder reflection and class-building hooks fail closed too.

Blind spots, recorded honestly (see the guide's *What this does not catch*):
**dynamic imports** — ``importlib.import_module("app.core.config")``,
``sys.modules[...]``, ``__import__`` — reach both the config module and the DB
regardless of P1/P2; computed reflection, ``exec``/``eval`` and frame
introspection are also outside this first-party guard rail. Imported library
code internals remain first-party review boundaries.
Ordinary singleton/function attributes and aliases are checked, not residuals. The scan
closes what syntax can close; it is not a sandbox.
"""

from __future__ import annotations

import ast
import pkgutil
import symtable
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

__all__ = [
    "ALLOWED_DEPLOYMENT_CONFIG",
    "ALLOWED_FRAMEWORK_IMPORTS",
    "CONNECTOR_ATTR",
    "FORBIDDEN_IMPORTS",
    "FORBIDDEN_SETTINGS",
    "Violation",
    "check_execution_context_prohibitions",
    "check_registry_enrollment",
    "connector_package_names",
    "connector_package_path",
    "connectors_root",
    "packages_under",
    "scan_package",
]

CONNECTOR_ATTR = "CONNECTOR"

# Import prefix → why a connector may not reach it (ADR-0019 §4). The reason is
# rendered into the failure message so the author is told what to do instead,
# not merely that something is banned.
FORBIDDEN_IMPORTS: dict[str, str] = {
    "app.services.secrets_service": (
        "the credential vault — the framework resolves the source's secret and "
        "hands you an already-authenticated client on ConnectorRun.http; a "
        "connector never sees a raw token"
    ),
    "app.db": (
        "Lumen's database (session / models / repositories) — connectors never "
        "read or write it; return state to the framework as FetchedDoc / "
        "SyncPage fields and let its per-page transaction persist it"
    ),
    "app.storage": (
        "Lumen's object store — persisting content is the framework's job; a "
        "connector returns bytes on FetchedDoc.data"
    ),
}

# Exact modules, never prefixes: derived from the shipped web/Drive imports.
# The conformance suite audits these modules and pure-domain modules for unsafe
# dependencies/reexports, so this cannot silently turn into another Settings or
# infrastructure seam. Config is a separate name/shape-sealed exception (P2).
ALLOWED_FRAMEWORK_IMPORTS: dict[str, str] = {
    "app.connectors.base": "connector protocols, run/ACL contexts, domain results and typed errors",
    "app.connectors.oauth": "OAuthSpec returned by Drive's oauth_spec capability",
    "app.core.logging": "get_logger for Drive's call-local operational logging",
    "app.net.egress": "shared SSRF address, DNS and IP-pinning helpers used by web",
}

# --- the settings seam -------------------------------------------------------
#
# Banning ``import app.db`` is not enough on its own:
# ``create_async_engine(get_settings().database_url)`` imports nothing forbidden
# and still opens a connection into Lumen's database. But *analysing the read*
# does not converge — receiver provenance has to cope with conditionals, walrus,
# tuple unpacking, ``d = dict(d)`` and cross-function laundering, and each round
# of that is wrong one construct deeper, in both directions at once.
#
# So the seam is sealed by SHAPE instead, which is the kind of property an AST
# can actually prove. A connector may touch settings in exactly one form::
#
#     get_settings().<field>
#
# The accessor may not be aliased, stored, passed, or transformed, and the read
# must terminate at the field. That single restriction kills every bypass class
# at once — you cannot launder an object you were never allowed to hold — and it
# has no false positives on a connector's own config, because the rule only ever
# looks at expressions rooted at ``get_settings``. ``connector_config.database_url``
# is invisible to it, which is exactly what the external-SQL allowance needs.
#
# Why not ban the accessor outright: ADR-0019 §4/§5 explicitly sanctions a
# connector reading its own deployment config — ``oauth_spec()`` reads the
# platform's OAuth client registration, and the ``web`` connector reads its
# User-Agent. Deployment config is the one non-secret surface a connector may
# read; Lumen's *infrastructure* fields below are not part of it.
SETTINGS_ACCESSOR = "get_settings"
SETTINGS_TYPE = "Settings"
CONFIG_MODULE = "app.core.config"

# --- the classification of every Settings field ------------------------------
#
# ``FORBIDDEN_SETTINGS`` are Lumen's own stateful-backend endpoints and all of
# its credentials/secrets — refused even through the one legal read shape.
# ``ALLOWED_DEPLOYMENT_CONFIG`` is every other field: deployment config a
# connector may read (ADR-0019 §4/§5). The two are asserted **disjoint and
# jointly exhaustive over the live ``Settings`` model** by a completeness test
# (``test_settings_classification_is_complete``), so this blacklist can never
# silently fall behind: add an infra field to ``Settings`` and forget to
# classify it and CI fails, forcing a human decision rather than a silent
# fail-open read. ``_accessor_misuse`` accepts only explicitly allowed fields;
# methods and unclassified attributes fail closed. Dynamic/reflective access
# remains review-caught under the first-party trust model.
FORBIDDEN_SETTINGS: dict[str, str] = {
    # Dedicated audit capacity is database infrastructure owned by app.db.
    "audit_db_pool_size": "Lumen's independent audit database pool capacity",
    "audit_db_pool_timeout_seconds": "Lumen's independent audit database acquisition deadline",
    "audit_db_operation_timeout_seconds": "Lumen's independent audit database operation deadline",
    # Stateful-backend endpoints owned by Lumen.
    "database_url": "Lumen's own database URL — connectors never open a connection to it",
    "redis_url": "Lumen's Redis (cache / broker / WS backplane)",
    "celery_broker_url": "Lumen's task broker — a connector does not enqueue its own work",
    "celery_result_backend": "Lumen's task result backend",
    "opensearch_url": "Lumen's search-store endpoint (the retrieval index, ADR-0010)",
    "s3_endpoint_url": "Lumen's object store — return bytes on FetchedDoc.data instead",
    "s3_public_endpoint_url": "Lumen's object store (browser-facing endpoint)",
    "sandbox_runner_url": "Lumen's code-sandbox runner service (ADR-0013)",
    # Infra identifiers that name Lumen's own stores.
    "s3_bucket": "Lumen's object-store bucket",
    "opensearch_index": "Lumen's search-store index name",
    # Credentials, secrets, and keys.
    "s3_access_key": "Lumen's object-store credential",
    "s3_secret_key": "Lumen's object-store credential",
    "opensearch_username": "Lumen's search-store credential",
    "opensearch_password": "Lumen's search-store credential",
    "openrouter_api_key": "Lumen's LLM-provider API key",
    "sandbox_runner_token": "Lumen's sandbox-runner credential — never connector-owned",
    "jwt_secret": "Lumen's token-signing key",
    "test_fast_password_hashing": "Lumen's password-hashing policy — never connector-owned",
    "secrets_encryption_key": (
        "the vault's master key — reading it is the secrets-service prohibition "
        "through another door"
    ),
}

# Every OTHER Settings field: deployment config a connector may legitimately read
# through ``get_settings().<field>`` (ADR-0019 §4/§5). Listed **explicitly**, not
# derived as "everything not forbidden", so that adding a field to ``Settings``
# lands it in *neither* set and trips the completeness test — the whole point is
# to force a conscious forbidden-or-allowed decision on each new field.
#
# NB ``gdrive_oauth_client_secret`` is here despite being a secret: it is the
# platform's OAuth *client* registration, which the ADR explicitly has the
# connector read in ``oauth_spec()`` (§1/§5). That is the connector's own
# deployment config, not Lumen's infrastructure.
ALLOWED_DEPLOYMENT_CONFIG: frozenset[str] = frozenset(
    {
        "access_token_ttl_seconds",
        "artifact_allowed_content_types",
        "artifact_retention_days",
        "chat_answer_max_tokens",
        "chat_max_tool_turns",
        "chat_model_registry",
        "chat_prompt_cache_enabled",
        "chat_shutdown_grace_seconds",
        "chat_suggestions_count",
        "chat_suggestions_enabled",
        "chat_suggestions_grace_seconds",
        "chat_suggestions_model",
        "chat_suggestions_timeout_seconds",
        "chat_summary_enabled",
        "chat_summary_keep_messages",
        "chat_summary_min_batch",
        "chat_summary_model",
        "chat_text_coalesce_chars",
        "chat_text_coalesce_seconds",
        "chat_tool_concurrency",
        "connector_acl_max_age_hours",
        "connector_ingest_recovery_batch",
        "connector_ingest_recovery_minutes",
        "connector_oauth_frontend_return_url",
        "connector_oauth_redirect_base_url",
        "connector_oauth_state_ttl_seconds",
        "connector_sync_interval_minutes",
        "context_compaction_chunk_size",
        "context_compaction_digest_chars",
        "context_fallback_max_input_tokens",
        "context_output_headroom_tokens",
        "context_proactive_compaction_enabled",
        "document_content_redirect",
        "document_text_max_bytes",
        "environment",
        "ffmpeg_path",
        "ffprobe_path",
        "gdrive_fetch_max_bytes",
        "gdrive_oauth_client_id",
        "gdrive_oauth_client_secret",
        "ingestion_chunk_overlap",
        "ingestion_chunk_size",
        "ingestion_embed_batch_size",
        "ingestion_max_retries",
        "ingestion_retry_backoff_seconds",
        "jwt_algorithm",
        "jwt_issuer",
        "llm_embed_cache_max_entries",
        "llm_embed_cache_ttl_seconds",
        "llm_embedding_api_base",
        "llm_embedding_dimensions",
        "llm_embedding_model",
        "llm_interactive_max_attempts",
        "llm_interactive_timeout_seconds",
        "llm_model",
        "llm_terminal_publish_margin_seconds",
        "llm_timeout_seconds",
        "log_level",
        "logo_allowed_content_types",
        "max_artifact_bytes",
        "max_logo_bytes",
        "max_media_upload_bytes",
        "max_upload_bytes",
        "mcp_allowed_transports",
        "mcp_call_timeout_seconds",
        "mcp_connect_timeout_seconds",
        "mcp_endpoint_allowlist",
        "mcp_rate_max_per_window",
        "mcp_rate_window_seconds",
        "media_max_duration_seconds",
        # Non-secret CPU/resource limits, not stateful infrastructure or credentials.
        "native_ingestion_threads",
        "native_doc_accept",
        "native_doc_shadow",
        "native_doc_cutover",
        "native_ppt_accept",
        "native_ppt_shadow",
        "native_ppt_cutover",
        "native_ingestion_max_documents",
        "native_ingestion_max_memory_bytes",
        "native_ingestion_max_input_bytes",
        "native_ingestion_max_output_chars",
        "native_ingestion_max_work_units",
        "native_ingestion_timeout_ms",
        "native_ingestion_tokenizer_path",
        "native_ingestion_tokenizer_sha256",
        "native_ingestion_tokenizer_model",
        "native_ingestion_chunk_tokens",
        "native_ingestion_chunk_chars",
        "native_ingestion_overlap_chars",
        "ingestion_checkpoint_max_output_bytes",
        "opensearch_timeout_seconds",
        "redbeat_key_prefix",
        "redbeat_lock_timeout_seconds",
        "refresh_token_ttl_seconds",
        "run_digest_interval_seconds",
        "run_max_in_flight_per_tenant",
        "run_max_retries",
        "run_rate_backoff_seconds",
        "run_rate_max_per_window",
        "run_rate_window_seconds",
        "run_retry_backoff_seconds",
        "s3_cors_allowed_origins",
        "s3_cors_managed_externally",
        "s3_incomplete_multipart_cleanup_managed_externally",
        "s3_presign_ttl_seconds",
        "sandbox_cpus",
        "sandbox_daily_runtime_seconds_per_tenant",
        "sandbox_enabled",
        "sandbox_image",
        "sandbox_max_concurrent_per_tenant",
        "sandbox_memory_bytes",
        "sandbox_output_bytes_cap",
        "sandbox_pids_limit",
        "sandbox_preinstalled_packages",
        "sandbox_runtime",
        "sandbox_scratch_bytes",
        "sandbox_session_limits_enabled",
        "sandbox_wall_clock_seconds",
        "search_direct_answer_max_tokens",
        "service_name",
        "source_sync_rate_backoff_seconds",
        "source_sync_rate_max_per_window",
        "source_sync_rate_window_seconds",
        "transcription_base_url",
        "transcription_chunk_overlap_seconds",
        "transcription_chunk_seconds",
        "transcription_model",
        "transcription_provider_options_json",
        "transcription_require_diarization",
        "transcription_timeout_seconds",
        "upload_allowed_content_types",
        "upload_incomplete_lifecycle_days",
        "upload_janitor_batch_size",
        "upload_janitor_interval_seconds",
        "upload_max_parts",
        "upload_part_size_bytes",
        "upload_session_ttl_seconds",
        "upload_sign_batch_size",
        "version",
        "web_search_default_k",
        "web_search_enabled",
        "web_search_endpoint",
        "web_search_fetch_top_n",
        "web_search_max_k",
        "web_search_rate_max_per_window",
        "web_search_rate_window_seconds",
        "web_search_timeout_seconds",
        "web_user_agent",
    }
)

# Method names that mutate their receiver in place. Calling one of these on a
# module-level name is the cross-run state the ADR forbids.
_MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "add",
        "discard",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
        "update",
        "setdefault",
        "popitem",
        "__setitem__",
    }
)

# Every node that opens a new lexical scope in Python 3.
_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


@dataclass(frozen=True, slots=True)
class Violation:
    """One prohibition breach, located precisely enough to fix."""

    rule: str
    module: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} [{self.rule}] {self.detail}"


# --- package discovery + enrollment ------------------------------------------


def connectors_root() -> Path:
    """Filesystem path of the ``app.connectors`` package."""
    spec = find_spec("app.connectors")
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    assert locations, "app.connectors is not an importable package"
    return Path(locations[0])


def connector_package_path(name: str) -> Path:
    """Filesystem path of ``app.connectors.<name>`` (no import of its contents).

    Resolved through the import system rather than guessed from ``__file__`` so
    the scan follows the same package the registry discovered.
    """
    spec = find_spec(f"app.connectors.{name}")
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    assert locations, (
        f"connector {name!r} is not an importable package — a connector is a "
        f"drop-in package app/connectors/{name}/ exposing CONNECTOR (ADR-0008 §3)"
    )
    return Path(locations[0])


def packages_under(search_paths: Sequence[str]) -> list[str]:
    """Subpackages of ``search_paths`` — the registry's discovery rule, verbatim.

    Deliberately the same ``pkgutil.iter_modules(...) if info.ispkg`` the
    registry performs, and **no extra conventions of our own**: no skipping
    ``_``-prefixed directories, no "looks like a connector" heuristic. Two
    discovery sets that differ by even one rule let a package exist in one view
    and not the other — which is precisely how a malformed ``foo`` hides behind
    a conforming ``_alias`` that registers under the name ``foo``.

    Split out from :func:`connector_package_names` so this rule is testable
    against a synthetic tree rather than only against whatever the repo happens
    to contain today.
    """
    return sorted(info.name for info in pkgutil.iter_modules(list(search_paths)) if info.ispkg)


def connector_package_names() -> list[str]:
    """The registry's **exact** package universe for ``app.connectors``."""
    import app.connectors as package

    return packages_under(list(package.__path__))


def check_registry_enrollment(
    packages: Sequence[str],
    *,
    load: Callable[[str], object | None],
    resolve: Callable[[str], object | None],
    check_surface: Callable[[object, str], None],
) -> None:
    """Every connector package's **own** ``CONNECTOR`` is the object that enrolled.

    ``registry._registry()`` *skips* a ``CONNECTOR`` that fails the runtime
    ``Connector`` protocol check rather than raising, so an incomplete new
    connector vanishes from ``registered_types()`` instead of failing. A suite
    parametrized over the registry would then pass green while the connector the
    author just added is not tested at all.

    Checking "is this directory's name among the registry's keys" does **not**
    close that. Two things defeat it, and both are real:

    * the key can be present because a *different* package registered under that
      ``name`` — a conforming ``_alias`` claiming ``name="foo"`` covers for a
      malformed ``foo/``, and conformance then exercises ``_alias`` while the
      enrollment scan is satisfied by ``foo``;
    * a package whose ``CONNECTOR`` is junk (``object()``) passes as soon as its
      name happens to be a registry key, because nothing ever looks at the
      object.

    So each package is verified on the object itself: its ``CONNECTOR`` exists,
    **is conforming**, is named for its own directory, and is *identical* (``is``)
    to what the registry resolved for that name. ``load`` / ``resolve`` /
    ``check_surface`` are injected so the same rule runs against the real
    registry and against synthetic offenders.
    """
    assert packages, (
        "no connector packages discovered — the enrollment scan is looking in the "
        "wrong place, and would pass vacuously"
    )

    problems: list[str] = []
    for package in packages:
        try:
            candidate = load(package)
        except Exception as exc:  # noqa: BLE001 — an import fault IS the finding
            problems.append(
                f"`{package}` could not be imported ({type(exc).__name__}: {exc}) — the "
                "registry silently skips a package that raises on import, so this "
                "connector would simply not exist"
            )
            continue
        if candidate is None:
            problems.append(
                f"`{package}` has no module-level {CONNECTOR_ATTR} — a connector "
                "registers by drop-in (ADR-0008 §3): connectors/"
                f"{package}/__init__.py must expose {CONNECTOR_ATTR} (assigned there "
                "or re-exported from a submodule)"
            )
            continue
        try:
            check_surface(candidate, package)
        except AssertionError as exc:
            problems.append(
                f"`{package}`'s {CONNECTOR_ATTR} does not satisfy the connector protocol, "
                "so registry discovery SKIPPED it — an incomplete connector silently "
                "disappears instead of failing, and every registry-parametrized "
                f"conformance test would pass without ever seeing it.\n      cause: {exc}"
            )
            continue
        enrolled = resolve(package)
        if enrolled is None:
            problems.append(
                f"`{package}` exposes a conforming {CONNECTOR_ATTR} but the registry has "
                f"no entry for the name {package!r} — check that `name` equals the "
                "package directory name"
            )
        elif enrolled is not candidate:
            problems.append(
                f"the registry resolved a DIFFERENT object for the name {package!r} than "
                f"the one `{package}` exposes — another package is registering under "
                "this name and shadowing it, so conformance would exercise the impostor "
                "while this package goes untested"
            )

    assert not problems, (
        "connector packages do not match the registry (see "
        "docs/guides/building-a-connector.md):\n  " + "\n  ".join(problems)
    )


# --- module naming + imports -------------------------------------------------


def _module_name(package: Path, path: Path) -> tuple[str, bool]:
    """``(dotted name, is_package_init)`` for a file inside a connector package."""
    rel = path.relative_to(package).with_suffix("")
    is_init = rel.name == "__init__"
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join([f"app.connectors.{package.name}", *parts]), is_init


def _resolve_relative(module_name: str, is_init: bool, level: int) -> str | None:
    """The absolute base a ``from ...x import y`` resolves against.

    ``level=1`` is the containing package (the module itself when it *is* a
    package ``__init__``); each further dot climbs one more. Without this a
    relative import scans as **zero** imports — precisely the shape a smuggled
    lazy import takes.
    """
    parts = module_name.split(".")
    climb = level - 1 if is_init else level
    if climb > len(parts):
        return None
    kept = parts[: len(parts) - climb] if climb else parts
    return ".".join(kept) if kept else None


def _import_from_base(node: ast.ImportFrom, module_name: str, is_init: bool) -> str | None:
    """Normalize the base of an absolute or relative ``from`` import."""
    if node.level == 0:
        return node.module
    resolved = _resolve_relative(module_name, is_init, node.level)
    if resolved is None:
        return None
    return f"{resolved}.{node.module}" if node.module else resolved


def _imported_modules(tree: ast.AST, module_name: str, is_init: bool) -> Iterator[tuple[str, int]]:
    """Every absolute module name an import could name.

    Covers all the evasion shapes at once — module level or inside a function
    body, absolute or **relative**:

    * ``import X`` → ``X``;
    * ``from X import y`` → both ``X`` and ``X.y`` (the forbidden module is just
      as often the imported *name*, e.g. ``from app.services import
      secrets_service``);
    * ``from .x import y`` / ``from ...db import repositories`` → resolved
      against this module's own package first.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(node, module_name, is_init)
            if not base:
                continue
            yield base, node.lineno
            for alias in node.names:
                if alias.name != "*":
                    yield f"{base}.{alias.name}", node.lineno


def _forbidden_import(module: str) -> tuple[str, str] | None:
    for prefix, reason in FORBIDDEN_IMPORTS.items():
        if module == prefix or module.startswith(prefix + "."):
            return prefix, reason
    return None


def _allowed_lumen_import(module: str, package_name: str) -> bool:
    own = f"app.connectors.{package_name}"
    return (
        module == own
        or module.startswith(own + ".")
        or module == "app.domain"
        or module.startswith("app.domain.")
        or module in ALLOWED_FRAMEWORK_IMPORTS
        or module == CONFIG_MODULE  # P2 limits this to unaliased get_settings.
    )


def _disallowed_imports(
    tree: ast.AST, module_name: str, is_init: bool, package_name: str
) -> Iterator[tuple[int, str]]:
    """Check static import targets; from-import members may name a module.

    An allowed leaf module may export ordinary symbols. An unlisted parent
    package may only select an allowed child (``from app.connectors import
    base``); it cannot export arbitrary helpers. No parent or descendant prefix
    is implicitly trusted. Relative imports share P1/P2's normalization.
    """
    for node in ast.walk(tree):
        targets: list[str]
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.") and alias.asname is None:
                    yield (
                        node.lineno,
                        (
                            f"imports `{alias.name}` without an alias, binding the root `app` "
                            f"namespace — use `import {alias.name} as module` or "
                            f"`from {alias.name} import Name` for an allowlisted module"
                        ),
                    )
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(node, module_name, is_init)
            if base is None:
                yield node.lineno, "relative import cannot be resolved within app"
                continue
            if _allowed_lumen_import(base, package_name):
                continue
            # Standard-library and third-party absolute imports remain legal.
            if node.level == 0 and base != "app" and not base.startswith("app."):
                continue
            targets = [f"{base}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "app":
                yield (
                    node.lineno,
                    (
                        f"uses root-qualified `app.{node.attr}` — use an aliased allowlisted "
                        "module or a from-import instead of the root `app` namespace"
                    ),
                )
            continue
        else:
            continue
        for target in targets:
            if (target == "app" or target.startswith("app.")) and not _allowed_lumen_import(
                target, package_name
            ):
                yield node.lineno, f"imports unlisted in-repo dependency `{target}`"


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """Child-id → parent. One pass, no flow analysis — just structure."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _accessor_misuse(name: ast.Name, parents: dict[int, ast.AST]) -> str | None:
    """Why this ``get_settings`` reference is not the one legal read shape.

    The whole seam in one function. ``get_settings().<field>`` is allowed and
    everything else is refused, so there is never a settings object in scope to
    launder — which is why no conditional, walrus, unpacking, ``dict(d)``, or
    cross-function trick needs its own rule. They are all the same violation:
    holding the object at all.
    """
    call = parents.get(id(name))
    if not (isinstance(call, ast.Call) and call.func is name):
        return (
            "references `get_settings` without calling it — the accessor may not be "
            "aliased, stored, or passed; read the field you need as `get_settings().<field>`"
        )
    attr = parents.get(id(call))
    if not (isinstance(attr, ast.Attribute) and attr.value is call):
        return (
            "binds, passes, or transforms the settings object — a connector reads ONE "
            "field directly (`get_settings().<field>`) and never holds the object, so "
            "there is nothing to launder into a Lumen infrastructure read"
        )
    if not isinstance(attr.ctx, ast.Load):
        return (
            f"deployment-config `.{attr.attr}` is read-only — assignment, augmentation "
            "and deletion are forbidden"
        )
    after = parents.get(id(attr))
    if isinstance(after, ast.Call) and after.func is attr:
        return (
            f"calls `.{attr.attr}()` on the settings object — the read must terminate at "
            "a field, so a flattening call like `.model_dump()` is refused"
        )
    if isinstance(after, ast.Subscript | ast.Attribute) and after.value is attr:
        return (
            f"keeps reading past `.{attr.attr}` — the read must terminate at a single "
            "deployment-config field"
        )
    if attr.attr.startswith("__"):
        return f"reads the dunder `{attr.attr}` off the settings object"
    if attr.attr in FORBIDDEN_SETTINGS:
        return (
            f"reads Lumen's `{attr.attr}` ({FORBIDDEN_SETTINGS[attr.attr]}) — deployment "
            "config for your own connector is fair game; Lumen's infrastructure is not"
        )
    if attr.attr not in ALLOWED_DEPLOYMENT_CONFIG:
        return (
            f"`.{attr.attr}` is not an allowed deployment-config field — methods and "
            "other settings-object attributes may retain or expose the settings object; "
            "read a field classified in ALLOWED_DEPLOYMENT_CONFIG"
        )
    return None


def _settings_seam(tree: ast.AST, module_name: str, is_init: bool) -> Iterator[tuple[int, str]]:
    """Every breach of the sealed settings seam, as ``(line, detail)``.

    Three name-level rules and one shape rule, all decidable from the syntax
    tree with no dataflow:

    1. ``from app.core.config import …`` may import **only** ``get_settings``,
       unaliased (an alias would defeat rule 4 by renaming the accessor);
    2. the config module may not be imported wholesale;
    3. the ``Settings`` *type* may not be referenced at all — a connector never
       constructs or annotates settings;
    4. every ``get_settings`` reference must be the one legal read shape
       (:func:`_accessor_misuse`).

    Relative imports use the same normalization as P1; imports of ``config``
    through its parent and module-qualified accessor references are refused.
    """
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = _import_from_base(node, module_name, is_init)
            for alias in node.names:
                if base == CONFIG_MODULE and (alias.name != SETTINGS_ACCESSOR or alias.asname):
                    spelled = alias.name + (f" as {alias.asname}" if alias.asname else "")
                    yield (
                        node.lineno,
                        f"imports `{spelled}` from {CONFIG_MODULE} — a connector may import "
                        f"only `{SETTINGS_ACCESSOR}`, unaliased",
                    )
                elif base and f"{base}.{alias.name}" == CONFIG_MODULE:
                    yield (
                        node.lineno,
                        f"imports `{CONFIG_MODULE}` wholesale — use "
                        f"`from {CONFIG_MODULE} import {SETTINGS_ACCESSOR}`",
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == CONFIG_MODULE or alias.name.startswith(CONFIG_MODULE + "."):
                    yield (
                        node.lineno,
                        f"imports `{alias.name}` wholesale — use "
                        f"`from {CONFIG_MODULE} import {SETTINGS_ACCESSOR}`",
                    )
        elif isinstance(node, ast.Name) and node.id == SETTINGS_TYPE:
            yield (
                node.lineno,
                f"references the `{SETTINGS_TYPE}` type — a connector never constructs or "
                "annotates a settings object",
            )
        elif isinstance(node, ast.Attribute) and node.attr == SETTINGS_TYPE:
            yield (
                node.lineno,
                f"references the `{SETTINGS_TYPE}` type — a connector never constructs or "
                "annotates a settings object",
            )
        elif isinstance(node, ast.Attribute) and node.attr == SETTINGS_ACCESSOR:
            yield (
                node.lineno,
                "references module-qualified `get_settings` — import the accessor "
                f"unaliased from {CONFIG_MODULE} and read `get_settings().<field>`",
            )
        elif isinstance(node, ast.Name) and node.id == SETTINGS_ACCESSOR:
            misuse = _accessor_misuse(node, parents)
            if misuse is not None:
                yield node.lineno, misuse


# --- module-level state ------------------------------------------------------


def _module_level_names(tree: ast.Module) -> frozenset[str]:
    """Let Python classify every binding form, without descending into local scopes."""
    symbols = symtable.symtable(ast.unparse(tree), "<connector>", "exec").get_symbols()
    return frozenset(
        symbol.get_name() for symbol in symbols if symbol.is_assigned() or symbol.is_imported()
    )


def _binding_values(target: ast.expr, value: ast.expr) -> Iterator[tuple[str, ast.expr]]:
    """Only name bindings and equal-arity, unstarred tuple displays are legal."""
    if isinstance(target, ast.Name):
        yield target.id, value
        return
    if (
        isinstance(target, ast.Tuple)
        and isinstance(value, ast.Tuple)
        and len(target.elts) == len(value.elts)
        and not any(isinstance(n, ast.Starred) for n in (*target.elts, *value.elts))
    ):
        for child, bound in zip(target.elts, value.elts, strict=True):
            yield from _binding_values(child, bound)
        return
    # Never silently lose targets when unpacking cannot match the grammar.
    unknown = ast.Call(func=ast.Name(id="<forbidden-binding>"), args=[], keywords=[])
    for child in ast.walk(target):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            yield child.id, unknown


def _walk_own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """``ast.walk`` that stops at every nested scope boundary."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES):
            yield child  # its name can bind here; its body cannot
            continue
        yield from _walk_own_scope(child)


def _own_scope_bindings(node: ast.AST) -> frozenset[str]:
    """Names bound in **this** scope only — nested scopes excluded.

    Scope-correctness is load-bearing: walking into nested functions would let a
    harmless ``CACHE = set()`` inside an inner helper suppress a genuine
    ``CACHE.add(...)`` in the enclosing one, which is exactly the mutation the
    rule exists to catch.
    """
    names: set[str] = set()

    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = node.args
        for arg in (
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg else []),
            *([args.kwarg] if args.kwarg else []),
        ):
            names.add(arg.arg)
    if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
        for generator in node.generators:
            for sub in ast.walk(generator.target):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
        return frozenset(names)

    raw_body = getattr(node, "body", [])
    statements = raw_body if isinstance(raw_body, list) else [raw_body]
    declared_global: set[str] = set()
    for statement in statements:
        # A nested def/class binds only its NAME here; its body is another scope.
        # Descending into it is precisely the bug that let an inner `CACHE = ...`
        # suppress a genuine `CACHE.add(...)` in the enclosing function.
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(statement.name)
            continue
        for child in _walk_own_scope(statement):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(child.name)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.Import | ast.ImportFrom):
                for alias in child.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(child, ast.Global | ast.Nonlocal):
                declared_global.update(child.names)
    # A `global X` makes X module state inside this scope, never a local.
    return frozenset(names - declared_global)


class _ScopeWalker(ast.NodeVisitor):
    """Finds writes to module-level names, honouring lexical scope.

    Scopes are tagged, because **a class namespace is not an enclosing scope**.
    A method does not close over its class body: in::

        CACHE = set()
        class C:
            CACHE = set()
            def mutate(self): CACHE.add("x")   # ← the MODULE global

    the unqualified ``CACHE`` inside ``mutate`` resolves to the module global,
    not to ``C.CACHE`` — so that is a real cross-run mutation, and treating the
    class body as an enclosing scope would silently excuse it.
    """

    def __init__(self, module_names: frozenset[str]) -> None:
        self.module_names = module_names
        # (kind, names) where kind ∈ {"function", "class", "comprehension"}.
        self.scopes: list[tuple[str, frozenset[str]]] = []
        self.findings: list[tuple[str, int]] = []

    def _shadowed(self, name: str) -> bool:
        for depth, (kind, names) in enumerate(reversed(self.scopes)):
            # Python's rule, exactly: a class namespace is visible only to code
            # running directly in the class body (depth 0), never to a nested
            # function or comprehension.
            if kind == "class" and depth > 0:
                continue
            if name in names:
                return True
        return False

    def _is_module_state(self, name: str) -> bool:
        return name in self.module_names and not self._shadowed(name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_defaults(node.args)
        self.scopes.append(("function", _own_scope_bindings(node)))
        self.visit(node.body)
        self.scopes.pop()

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        # A comprehension is its own scope in Python 3: its targets shadow there
        # and nowhere else. Its FIRST iterable evaluates in the enclosing scope.
        first, *remaining = node.generators
        self.visit(first.iter)
        self.scopes.append(("comprehension", _own_scope_bindings(node)))
        self.visit(first.target)
        for condition in first.ifs:
            self.visit(condition)
        for generator in remaining:
            self.visit(generator)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        self.scopes.append(("class", _own_scope_bindings(node)))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators and defaults evaluate in the ENCLOSING scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_defaults(node.args)
        for arg in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *([node.args.vararg] if node.args.vararg else []),
            *([node.args.kwarg] if node.args.kwarg else []),
        ):
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        self.scopes.append(("function", _own_scope_bindings(node)))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _visit_defaults(self, args: ast.arguments) -> None:
        for default in (*args.defaults, *args.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_Global(self, node: ast.Global) -> None:
        if self.scopes:  # a `global` at module level is a no-op
            for name in node.names:
                self.findings.append((f"`global {name}` rebinds module state", node.lineno))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._visit_store(node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._visit_store(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _MUTATING_METHODS:
            root = self._module_root(func.value)
            if root is not None:
                self.findings.append(
                    (f"`{ast.unparse(func)}(...)` mutates module state", node.lineno)
                )
        self.generic_visit(node)

    def _module_root(self, receiver: ast.expr) -> str | None:
        """Unwrap the whole static receiver chain; depth/type cannot hide its root."""
        while isinstance(receiver, ast.Attribute | ast.Subscript):
            receiver = receiver.value
        if isinstance(receiver, ast.Name) and self._is_module_state(receiver.id):
            return receiver.id
        return None

    def _visit_store(self, node: ast.Attribute | ast.Subscript) -> None:
        # Use Python's store/delete context, rather than enumerating statements.
        # This includes unpacking, comprehension targets, with-as and for targets.
        if isinstance(node.ctx, ast.Store | ast.Del):
            root = self._module_root(node)
            if root is not None:
                self.findings.append((f"writes to module-level `{root}`", node.lineno))


# --- the scan ----------------------------------------------------------------


class _PackageSymbols:
    """Read declarations, never evaluate connector expressions or imports."""

    def __init__(self, sources: dict[str, tuple[ast.Module, bool]]) -> None:
        self.local_modules = frozenset(sources)
        self.modules = {
            name: _ImmutableGrammar(tree, name, is_init, self)
            for name, (tree, is_init) in sources.items()
        }
        self.missing: set[str] = set()

    def module(self, name: str) -> _ImmutableGrammar | None:
        if name in self.modules:
            return self.modules[name]
        if name in self.missing:
            return None
        # Imported code is a declaration boundary, not an arbitrary imported
        # payload allowance. Read its source without importing/executing it.
        import sys

        self.missing.add(name)
        for root in sys.path:
            path = Path(root).joinpath(*name.split("."))
            for source, is_init in ((path.with_suffix(".py"), False), (path / "__init__.py", True)):
                if source.is_file():
                    try:
                        import tokenize

                        with tokenize.open(source) as stream:
                            tree = ast.parse(stream.read())
                    except (OSError, SyntaxError, UnicodeError):
                        return None
                    grammar = _ImmutableGrammar(tree, name, is_init, self)
                    self.modules[name] = grammar
                    return grammar
        return None

    def resolve(
        self, qualified: str, seen: frozenset[str] = frozenset()
    ) -> tuple[_ImmutableGrammar, str] | None:
        if qualified in seen:
            return None
        seen = seen | {qualified}
        parts = qualified.split(".")
        for size in range(len(parts), 0, -1):
            owner = self.module(".".join(parts[:size]))
            if owner is None:
                continue
            member = ".".join(parts[size:])
            root, _, tail = member.partition(".")
            if root in owner.imports and root not in owner.values:
                target = owner.imports[root] + (f".{tail}" if tail else "")
                return self.resolve(target, seen)
            return owner, member
        return None


def _declaration_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Module declarations only; TYPE_CHECKING is not runtime storage."""
    yield node
    if isinstance(node, _SCOPE_NODES):
        return
    if isinstance(node, ast.If) and (
        isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        or isinstance(node.test, ast.Attribute)
        and node.test.attr == "TYPE_CHECKING"
    ):
        for child in node.orelse:
            yield from _declaration_nodes(child)
        return
    for child in ast.iter_child_nodes(node):
        yield from _declaration_nodes(child)


class _ImmutableGrammar:
    """IMM is a strict syntax grammar. No result/factory/iterable inference."""

    def __init__(
        self, tree: ast.Module, module: str, is_init: bool, package: _PackageSymbols
    ) -> None:
        self.module = module
        self.package = package
        self.imports: dict[str, str] = {}
        self.module_imports: set[str] = set()
        self.values: dict[str, list[ast.expr]] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.functions: set[str] = set()
        for node in _declaration_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.module_imports.add(alias.asname or alias.name.split(".")[0])
                    self.imports[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom):
                base = _import_from_base(node, module, is_init)
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = f"{base}.{alias.name}"
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions.add(node.name)
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr | ast.AugAssign):
                if node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    value = node.value
                    if isinstance(node, ast.NamedExpr | ast.AugAssign) or len(targets) != 1:
                        value = ast.Call(
                            func=ast.Name(id="<forbidden-binding>"), args=[], keywords=[]
                        )
                    for target in targets:
                        for name, bound in _binding_values(target, value):
                            self.values.setdefault(name, []).append(bound)

    def qualified(self, node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return self.imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{self.qualified(node.value)}.{node.attr}"
        return ""

    def reference(self, node: ast.expr) -> tuple[_ImmutableGrammar, str] | None:
        qualified = self.qualified(node)
        if not qualified:
            return None
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id not in self.imports:
            qualified = f"{self.module}.{qualified}"
        return self.package.resolve(qualified)

    def dataclass(self, node: ast.ClassDef, *, frozen: bool = False) -> bool:
        return any(
            self.qualified(d.func if isinstance(d, ast.Call) else d) == "dataclasses.dataclass"
            and (
                not frozen
                or isinstance(d, ast.Call)
                and any(
                    k.arg == "frozen"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value is True
                    for k in d.keywords
                )
            )
            for d in node.decorator_list
        )

    def enum(self, node: ast.ClassDef, seen: frozenset[str] = frozenset()) -> bool:
        marker = f"{self.module}.{node.name}"
        if marker in seen:
            return False
        for base in node.bases:
            if self.qualified(base) in {
                "enum.Enum",
                "enum.IntEnum",
                "enum.StrEnum",
                "enum.Flag",
                "enum.IntFlag",
            }:
                return True
            ref = self.reference(base)
            if ref is not None:
                owner, name = ref
                if name in owner.classes and owner.enum(owner.classes[name], seen | {marker}):
                    return True
        return False

    def immutable_enum(self, node: ast.ClassDef, seen: frozenset[str]) -> bool:
        """Only package-visible Enum ancestry and IMM member payloads qualify."""
        marker = f"<enum:{self.module}.{node.name}>"
        if (
            self.module not in self.package.local_modules
            or marker in seen
            or node.keywords
            or any(
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                and child.name in {"__init_subclass__", "__set_name__", "__prepare__"}
                for child in node.body
            )
        ):
            return False
        seen = seen | {marker}
        for base in node.bases:
            if self.qualified(base) in {
                "enum.Enum",
                "enum.IntEnum",
                "enum.StrEnum",
                "enum.Flag",
                "enum.IntFlag",
                "str",
                "int",
                "object",
            }:
                continue
            ref = self.reference(base)
            if ref is None:
                return False
            owner, name = ref
            parent = owner.classes.get(name)
            if parent is None or not owner.enum(parent) or not owner.immutable_enum(parent, seen):
                return False
        return self.enum(node) and all(
            self.immutable(child.value, seen)
            for child in node.body
            if isinstance(child, ast.Assign | ast.AnnAssign) and child.value is not None
        )

    def classvar(self, annotation: ast.expr) -> bool:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return False
        return (
            self.qualified(
                annotation.value if isinstance(annotation, ast.Subscript) else annotation
            )
            == "typing.ClassVar"
        )

    def immutable_type(self, node: ast.expr, seen: frozenset[str]) -> bool:
        if isinstance(node, ast.Constant):
            if node.value is None or node.value is Ellipsis:
                return True
            if isinstance(node.value, str):
                try:
                    return self.immutable_type(ast.parse(node.value, mode="eval").body, seen)
                except SyntaxError:
                    return False
            return False
        if (
            isinstance(node, ast.Name)
            and node.id in {"str", "bytes", "int", "float", "complex", "bool", "tuple", "frozenset"}
            and node.id not in self.values
            and node.id not in self.functions
            and node.id not in self.classes
        ):
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.immutable_type(node.left, seen) and self.immutable_type(node.right, seen)
        if isinstance(node, ast.Subscript):
            if self.qualified(node.value) not in {
                "tuple",
                "frozenset",
                "typing.Tuple",
                "typing.FrozenSet",
                "types.MappingProxyType",
            }:
                return False
            elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            return all(self.immutable_type(element, seen) for element in elements)
        ref = self.reference(node)
        if ref is not None:
            owner, name = ref
            declaration = owner.classes.get(name)
            if declaration is not None:
                return True  # a type declaration, never permission to retain its instances
        return False

    def constant_expression(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return type(node.value) in {
                str,
                bytes,
                int,
                float,
                complex,
                bool,
                type(None),
                type(Ellipsis),
            }
        if isinstance(node, ast.UnaryOp):
            return self.constant_expression(node.operand)
        if isinstance(node, ast.BinOp):
            return self.constant_expression(node.left) and self.constant_expression(node.right)
        return False

    def immutable(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> bool:
        if isinstance(node, ast.Constant | ast.UnaryOp | ast.BinOp):
            return self.constant_expression(node)
        if isinstance(node, ast.Tuple):
            return all(
                not isinstance(v, ast.Starred) and self.immutable(v, seen) for v in node.elts
            )
        if isinstance(node, ast.Name | ast.Attribute):
            if (
                isinstance(node, ast.Name)
                and node.id in self.module_imports
                and node.id not in self.values
            ):
                return True
            ref = self.reference(node)
            if ref is None:
                return False
            owner, name = ref
            if not name or name in owner.functions or name in owner.classes:
                return True
            if name in owner.values and owner.module in self.package.local_modules:
                marker = f"{owner.module}.{name}"
                return marker not in seen and all(
                    owner.immutable(v, seen | {marker}) for v in owner.values[name]
                )
            class_name, _, member = name.partition(".")
            declaration = owner.classes.get(class_name)
            return (
                declaration is not None
                and owner.enum(declaration)
                and owner.immutable_enum(declaration, seen)
                and any(
                    isinstance(child, ast.Assign | ast.AnnAssign)
                    and any(
                        isinstance(t, ast.Name) and t.id == member
                        for t in (
                            child.targets if isinstance(child, ast.Assign) else [child.target]
                        )
                    )
                    for child in declaration.body
                )
            )
        if (
            not isinstance(node, ast.Call)
            or any(k.arg is None for k in node.keywords)
            or any(isinstance(a, ast.Starred) for a in node.args)
        ):
            return False
        constructor = self.qualified(node.func)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in set(self.values) | set(self.classes) | self.functions
        ):
            constructor = f"{self.module}.{node.func.id}"
        if constructor in {"frozenset", "builtins.frozenset"}:
            return (
                not node.keywords
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Set | ast.List | ast.Tuple)
                and all(
                    not isinstance(v, ast.Starred) and self.immutable(v, seen)
                    for v in node.args[0].elts
                )
            )
        if constructor == "types.MappingProxyType":
            return (
                not node.keywords
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Dict)
                and all(
                    k is not None and self.immutable(k, seen) and self.immutable(v, seen)
                    for k, v in zip(node.args[0].keys, node.args[0].values, strict=True)
                )
            )
        if constructor == "re.compile":
            return (
                not node.keywords
                and len(node.args) in {1, 2}
                and all(isinstance(a, ast.Constant) and self.immutable(a, seen) for a in node.args)
            )
        return False  # every other call, including every record constructor, fails closed


class _ImmutableBindings(_ScopeWalker):
    """Module scope (including module classes) is immutable by construction.

    The inherited mutation scan is secondary protection for imported code and
    class objects; container borrowing is irrelevant
    to the primary binding rule.
    """

    def __init__(self, tree: ast.Module, grammar: _ImmutableGrammar) -> None:
        super().__init__(_module_level_names(tree))
        self.values = grammar
        self.parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        self.class_nodes: list[ast.ClassDef] = []
        self.instance_receivers: list[str | None] = []
        self.attribute_setters = {"setattr", "delattr", "builtins.setattr", "builtins.delattr"}
        self.namespace_readers = {"vars", "builtins.vars"}
        self.attribute_readers = {"getattr", "builtins.getattr"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "builtins":
                self.attribute_setters.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in {"setattr", "delattr"}
                )
                self.namespace_readers.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "vars"
                )
                self.attribute_readers.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "getattr"
                )

    def _shared_scope(self) -> bool:
        return not any(kind == "function" for kind, _ in self.scopes)

    def _matches_grammar(self, value: ast.expr) -> bool:
        # IMM references are module-level declarations, never an enclosing
        # local/class name that merely shares a module constant's spelling.
        return self.values.immutable(value) and not any(
            isinstance(child, ast.Name) and self._shadowed(child.id) for child in ast.walk(value)
        )

    def _check(
        self, target: ast.expr, value: ast.expr, line: int, *, field_annotation: bool = False
    ) -> None:
        if self._shared_scope() or (self.scopes and self.scopes[-1][0] == "class"):
            for name, bound in _binding_values(target, value):
                if not self._matches_grammar(bound) and not self._metadata(bound, field_annotation):
                    self.findings.append(
                        (
                            f"module-level mutable container `{name}` or unproved value "
                            "must bind an immutable value — use tuples, frozensets or "
                            "MappingProxyType with immutable contents; keep run state local",
                            line,
                        )
                    )

    def _metadata(self, node: ast.expr, field_annotation: bool) -> bool:
        if not self.class_nodes or not isinstance(node, ast.Call):
            return False
        declaration = self.class_nodes[-1]
        constructor = self.values.qualified(node.func)
        if (
            field_annotation
            and constructor == "dataclasses.field"
            and self.values.dataclass(declaration)
        ):
            # A Field descriptor describes an instance default; it is not a
            # shared default_factory result. Mutable defaults remain forbidden.
            return not node.args and all(
                k.arg is not None
                and self.values.immutable(k.value)
                or k.arg == "default_factory"
                and isinstance(k.value, ast.Name | ast.Lambda)
                for k in node.keywords
            )
        return False

    def visit_If(self, node: ast.If) -> None:
        if self.values.qualified(node.test) == "typing.TYPE_CHECKING":
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(keyword.arg == "metaclass" for keyword in node.keywords):
            self.findings.append(
                (
                    "class-building metaclass= customisation is forbidden; "
                    "use ordinary class declarations",
                    node.lineno,
                )
            )
        self._check_decorators(node.decorator_list, class_definition=True)
        self.class_nodes.append(node)
        super().visit_ClassDef(node)
        self.class_nodes.pop()

    def _check_decorators(
        self, decorators: list[ast.expr], *, class_definition: bool = False
    ) -> None:
        allowed = (
            {"dataclasses.dataclass", "enum.unique"}
            if class_definition
            else {
                "staticmethod",
                "classmethod",
                "property",
                "abc.abstractmethod",
                "typing.overload",
            }
        )
        for decorator in decorators:
            function = decorator.func if isinstance(decorator, ast.Call) else decorator
            if self.values.qualified(function) not in allowed:
                self.findings.append(
                    (
                        f"decorator `{ast.unparse(function)}` may retain cross-call state — "
                        "memoizers (cache/lru_cache/cached_property) and unproved decorators "
                        "are forbidden; keep caching inside the run",
                        decorator.lineno,
                    )
                )

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name in {"__dict__", "__setattr__", "__delattr__", "__class__"}:
            self._reflection(node.name, node.lineno)
        if self.class_nodes and node.name in {"__init_subclass__", "__set_name__", "__prepare__"}:
            self.findings.append(
                (
                    f"class-building hook `{node.name}` is forbidden; keep state local to a run",
                    node.lineno,
                )
            )
        self._check_decorators(node.decorator_list)
        args = [*node.args.posonlyargs, *node.args.args]
        receiver = None
        if (
            self.class_nodes
            and self.scopes
            and self.scopes[-1][0] == "class"
            and args
            and not any(
                self.values.qualified(d) in {"classmethod", "staticmethod"}
                for d in node.decorator_list
            )
            and not self.values.dataclass(self.class_nodes[-1], frozen=True)
            and not self.values.enum(self.class_nodes[-1])
            and not any(
                isinstance(child, ast.Name)
                and isinstance(child.ctx, ast.Store | ast.Del)
                and child.id == args[0].arg
                for statement in node.body
                for child in _walk_own_scope(statement)
            )
        ):
            # Mutable helper instances may exist only per call: their constructors
            # cannot pass the module-value proof. Keep the real HTML parser legal.
            receiver = args[0].arg
        self.instance_receivers.append(receiver)
        super()._visit_function(node)
        self.instance_receivers.pop()

    def _call_local_instance(self, value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Name)
            and bool(self.instance_receivers)
            and value.id == self.instance_receivers[-1]
        )

    def _visit_defaults(self, args: ast.arguments) -> None:
        for default in (*args.defaults, *args.kw_defaults):
            if default is not None and not self._matches_grammar(default):
                self.findings.append(
                    (
                        f"function/lambda default `{ast.unparse(default)}` must be provably "
                        "immutable at every nesting level — use None and create run state "
                        "inside the call",
                        default.lineno,
                    )
                )
        super()._visit_defaults(args)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_setter_reference(node)
        if node.attr in {"__dict__", "__setattr__", "__delattr__", "__class__"}:
            self._reflection(node.attr, node.lineno)
        if node.attr == "__slots__":
            self._reflection("__slots__ access/mutation", node.lineno)
        if self.values.qualified(node) in self.namespace_readers:
            self._reflection("vars", node.lineno)
        if isinstance(node.ctx, ast.Store | ast.Del) and not self._call_local_instance(node.value):
            self.findings.append(
                (
                    f"attribute write `{ast.unparse(node)}` may retain cross-call state "
                    "on a function, class or singleton; keep run state in local containers",
                    node.lineno,
                )
            )
        super().visit_Attribute(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            self.values.qualified(node.func) in self.attribute_readers | self.attribute_setters
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value
            in {"__dict__", "__setattr__", "__delattr__", "__class__", "__slots__"}
        ):
            self._reflection(str(node.args[1].value), node.lineno)
        if self.values.qualified(node.func) in self.attribute_setters and not (
            node.args and self._call_local_instance(node.args[0])
        ):
            self.findings.append(
                (
                    f"`{ast.unparse(node.func)}` may write retained function/class/singleton "
                    "attributes — keep run state in local containers",
                    node.lineno,
                )
            )
        super().visit_Call(node)

    def _reflection(self, name: str, line: int) -> None:
        self.findings.append(
            (
                f"reflection `{name}` is forbidden in connector packages; use call-local values "
                "and snapshot serializers instead of a live object namespace",
                line,
            )
        )

    def visit_Name(self, node: ast.Name) -> None:
        self._check_setter_reference(node)
        if node.id in {"__dict__", "__setattr__", "__delattr__", "__class__"}:
            self._reflection(node.id, node.lineno)
        if self.values.qualified(node) in self.namespace_readers:
            self._reflection("vars", node.lineno)

    def _check_setter_reference(self, node: ast.Name | ast.Attribute) -> None:
        if self.values.qualified(node) not in self.attribute_setters:
            return
        parent = self.parents.get(node)
        if not (isinstance(parent, ast.Call) and parent.func is node):
            self._reflection("setattr/delattr reference", node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in {"__dict__", "__setattr__", "__delattr__", "__class__"} or (
                node.module == "builtins" and alias.name == "vars"
            ):
                self._reflection(alias.name, node.lineno)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del) and any(
            isinstance(child, ast.Attribute) and child.attr == "__slots__"
            for child in ast.walk(node.value)
        ):
            self._reflection("__slots__ mutation", node.lineno)
        super().visit_Subscript(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Subscript | ast.BinOp)
            and self.values.immutable_type(node.value, frozenset())
        ):
            return  # unambiguous implicit type alias; not an instance value
        value = (
            node.value
            if len(node.targets) == 1
            else ast.Call(func=ast.Name(id="<chained-assignment>"), args=[], keywords=[])
        )
        for target in node.targets:
            self._check(target, value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self.values.qualified(node.annotation) == "typing.TypeAlias":
            return
        if node.value is not None:
            self._check(
                node.target,
                node.value,
                node.lineno,
                field_annotation=not self.values.classvar(node.annotation),
            )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._unknown_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._unknown_target(node.target, node.lineno)
        self.generic_visit(node)

    def _unknown_target(self, target: ast.expr, line: int) -> None:
        self._check(target, ast.Call(func=ast.Name(id="<unknown>"), args=[], keywords=[]), line)

    def visit_For(self, node: ast.For) -> None:
        self._unknown_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._unknown_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._unknown_target(item.optional_vars, node.lineno)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._unknown_target(item.optional_vars, node.lineno)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._unknown_target(ast.Name(id=node.name), node.lineno)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self._unknown_target(ast.Name(id=node.name), node.lineno)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._unknown_target(ast.Name(id=node.name), node.lineno)
        self.generic_visit(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._unknown_target(ast.Name(id=node.rest), node.lineno)
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self.findings.append((f"`nonlocal {name}` may rebind shared state", node.lineno))


def scan_package(package: Path) -> list[Violation]:
    """Every prohibition breach in a connector package (empty = conformant)."""
    violations: list[Violation] = []
    sources: dict[str, tuple[ast.Module, bool]] = {}
    for path in sorted(p for p in package.rglob("*.py") if p.is_file()):
        module, is_init = _module_name(package, path)
        sources[module] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), is_init
    symbols = _PackageSymbols(sources)
    for module, (tree, is_init) in sources.items():
        for line, detail in _disallowed_imports(tree, module, is_init, package.name):
            violations.append(
                Violation(
                    rule="connector-import-allowlist",
                    module=module,
                    line=line,
                    detail=f"{detail} — the connector import allowlist permits only its own "
                    "package, app.domain, the exact ALLOWED_FRAMEWORK_IMPORTS modules "
                    "and the sealed config accessor; use ConnectorRun/domain values "
                    "instead of API, service or infrastructure dependencies",
                )
            )
        for imported, line in _imported_modules(tree, module, is_init):
            hit = _forbidden_import(imported)
            if hit is not None:
                prefix, reason = hit
                violations.append(
                    Violation(
                        rule="no-vault-no-db",
                        module=module,
                        line=line,
                        detail=f"imports `{imported}` ({prefix}): {reason}",
                    )
                )
        for line, detail in _settings_seam(tree, module, is_init):
            violations.append(
                Violation(rule="settings-seam", module=module, line=line, detail=detail)
            )
        walker = _ImmutableBindings(tree, symbols.modules[module])
        walker.visit(tree)
        for detail, line in walker.findings:
            violations.append(
                Violation(rule="no-mutable-module-state", module=module, line=line, detail=detail)
            )
    return violations


def check_execution_context_prohibitions(package: Path) -> None:
    """Assert a connector package obeys the §4 execution-context prohibitions."""
    violations = scan_package(package)
    assert not violations, (
        f"connector package `{package.name}` breaks the ADR-0019 §4 execution-context "
        "prohibitions (connectors never touch the vault, Lumen's DB, or mutable module "
        "state — see docs/guides/building-a-connector.md):\n  "
        + "\n  ".join(str(v) for v in violations)
    )
