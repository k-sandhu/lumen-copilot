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

**P1 — forbidden imports** (:data:`FORBIDDEN_IMPORTS`): Lumen's own persistence
and credential seams — the secrets vault, the DB session/repositories/models,
the object store. Deliberately **not** banned: ``sqlalchemy`` itself. The ADR
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
so there is nothing to launder: every bypass class dies at once rather than one
per round. And because the rule only inspects expressions rooted at
``get_settings``, a connector's own ``connector_config.database_url`` is
invisible to it — the external-SQL allowance survives by construction.

Deployment config stays readable, because ADR-0019 §4/§5 explicitly sanctions
it (``oauth_spec()`` reads the platform's OAuth client registration; ``web``
reads its User-Agent). Lumen's *infrastructure* fields
(:data:`FORBIDDEN_SETTINGS`) are refused even through the one legal shape.

**P3 — no mutable module-level state**, detected as *mutation*, not as
container type: a module-level lookup table (``_EXPORT_MIME = {...}``) that is
only ever read is a constant, while ``_cache = {}`` plus a ``.setdefault`` in a
method is cross-run state. The scope analysis is genuinely lexical
(:class:`_ScopeWalker`): a binding inside a *nested* function does not suppress a
mutation in the enclosing one, and a **class body is not an enclosing scope** —
a method's unqualified name resolves past its class straight to the module
global, so `class C: CACHE = set()` does not excuse `CACHE.add(...)` in a method.

Blind spots, recorded honestly (see the guide's *What this does not catch*):
**dynamic imports** — ``importlib.import_module("app.core.config")``,
``sys.modules[...]``, ``__import__`` — reach both the config module and the DB
regardless of P1/P2, and are the single residue of the settings seam; plus
state held on a connector *instance* attribute, and mutation reached through an
alias rather than the module-level name. Those stay review-caught, which is
adequate under ADR-0019 §4's first-party, code-reviewed trust model. The scan
closes what syntax can close; it is not a sandbox.
"""

from __future__ import annotations

import ast
import pkgutil
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

__all__ = [
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

# Settings attributes that ARE Lumen's own infrastructure and credentials
# (`app/core/config.py`) — refused even through the one legal read shape.
FORBIDDEN_SETTINGS: dict[str, str] = {
    "database_url": "Lumen's own database URL — connectors never open a connection to it",
    "redis_url": "Lumen's Redis (cache / broker / WS backplane)",
    "celery_broker_url": "Lumen's task broker — a connector does not enqueue its own work",
    "celery_result_backend": "Lumen's task result backend",
    "s3_endpoint_url": "Lumen's object store — return bytes on FetchedDoc.data instead",
    "s3_access_key": "Lumen's object-store credential",
    "s3_secret_key": "Lumen's object-store credential",
    "s3_bucket": "Lumen's object-store bucket",
    "jwt_secret": "Lumen's token-signing key",
    "secrets_encryption_key": (
        "the vault's master key — reading it is the secrets-service prohibition "
        "through another door"
    ),
}

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
        "update",
        "setdefault",
        "popitem",
        "__setitem__",
    }
)

# Node types that *are* a mutable literal when bound at module level.
_MUTABLE_LITERALS = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)

# Callables whose result is a fresh mutable container.
_MUTABLE_FACTORIES = frozenset({"list", "dict", "set", "defaultdict", "OrderedDict", "Counter"})

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
            if node.level == 0:
                base = node.module or ""
            else:
                resolved = _resolve_relative(module_name, is_init, node.level)
                if resolved is None:
                    continue
                base = f"{resolved}.{node.module}" if node.module else resolved
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
    after = parents.get(id(attr))
    if isinstance(after, ast.Call) and after.func is attr:
        return (
            f"calls `.{attr.attr}()` on the settings object — the read must terminate at "
            "a field, so a flattening call like `.model_dump()` is refused"
        )
    if isinstance(after, ast.Subscript | ast.Attribute):
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
    return None


def _settings_seam(tree: ast.AST) -> Iterator[tuple[int, str]]:
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
    """
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == CONFIG_MODULE:
            for alias in node.names:
                if alias.name != SETTINGS_ACCESSOR or alias.asname:
                    spelled = alias.name + (f" as {alias.asname}" if alias.asname else "")
                    yield (
                        node.lineno,
                        f"imports `{spelled}` from {CONFIG_MODULE} — a connector may import "
                        f"only `{SETTINGS_ACCESSOR}`, unaliased",
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
        elif isinstance(node, ast.Name) and node.id == SETTINGS_ACCESSOR:
            misuse = _accessor_misuse(node, parents)
            if misuse is not None:
                yield node.lineno, misuse


# --- module-level state ------------------------------------------------------


def _module_level_names(tree: ast.Module) -> frozenset[str]:
    """Names bound by a top-level assignment (the candidate module state)."""
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return frozenset(names)


def _is_constant_cased(name: str) -> bool:
    """``_EXPORT_MIME`` / ``API_BASE`` yes; ``_cache`` / ``registry`` no."""
    stripped = name.strip("_")
    return bool(stripped) and stripped.isupper()


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _mutable_literal_state(tree: ast.Module) -> Iterator[tuple[str, int]]:
    """Module-level mutable containers bound to a *non-constant* name.

    ``__all__ = [...]`` (dunder) and ``_EXPORT_MIME = {...}`` (constant-cased)
    are read-only tables and pass; ``_cache = {}`` is the shape every accidental
    cross-run cache is written in and fails.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        is_mutable = isinstance(value, _MUTABLE_LITERALS) or (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in _MUTABLE_FACTORIES
        )
        if not is_mutable:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if _is_dunder(target.id) or _is_constant_cased(target.id):
                continue
            yield target.id, node.lineno


def _walk_own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """``ast.walk`` that stops at every nested scope boundary."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES):
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
                if isinstance(sub, ast.Name):
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
        self.scopes.append(("function", _own_scope_bindings(node)))
        self.visit(node.body)
        self.scopes.pop()

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        # A comprehension is its own scope in Python 3: its targets shadow there
        # and nowhere else — and, like a function, it does not see a class body.
        self.scopes.append(("comprehension", _own_scope_bindings(node)))
        self.generic_visit(node)
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
        self.scopes.append(("class", _own_scope_bindings(node)))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators and defaults evaluate in the ENCLOSING scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.scopes.append(("function", _own_scope_bindings(node)))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def visit_Global(self, node: ast.Global) -> None:
        if self.scopes:  # a `global` at module level is a no-op
            for name in node.names:
                self.findings.append((f"`global {name}` rebinds module state", node.lineno))

    def visit_Assign(self, node: ast.Assign) -> None:
        self._targets(node.targets, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._targets([node.target], node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._targets([node.target], node.lineno)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        self._targets(node.targets, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            self.scopes
            and isinstance(func, ast.Attribute)
            and func.attr in _MUTATING_METHODS
            and isinstance(func.value, ast.Name)
            and self._is_module_state(func.value.id)
        ):
            self.findings.append(
                (f"`{func.value.id}.{func.attr}(...)` mutates module state", node.lineno)
            )
        self.generic_visit(node)

    def _targets(self, targets: list[ast.expr], lineno: int) -> None:
        if not self.scopes:
            return  # module-level initialisation, not cross-run state
        for target in targets:
            if isinstance(target, ast.Subscript | ast.Attribute):
                inner = target.value
                if isinstance(inner, ast.Name) and self._is_module_state(inner.id):
                    self.findings.append((f"writes to module-level `{inner.id}`", lineno))


# --- the scan ----------------------------------------------------------------


def scan_package(package: Path) -> list[Violation]:
    """Every prohibition breach in a connector package (empty = conformant)."""
    violations: list[Violation] = []
    for path in sorted(p for p in package.rglob("*.py") if p.is_file()):
        module, is_init = _module_name(package, path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
        for line, detail in _settings_seam(tree):
            violations.append(
                Violation(rule="settings-seam", module=module, line=line, detail=detail)
            )
        for name, line in _mutable_literal_state(tree):
            violations.append(
                Violation(
                    rule="no-mutable-module-state",
                    module=module,
                    line=line,
                    detail=(
                        f"module-level mutable container `{name}` — a connector "
                        "holds nothing between runs; keep per-run state on the "
                        "stack (or name it a real constant if it is read-only)"
                    ),
                )
            )
        walker = _ScopeWalker(_module_level_names(tree))
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
