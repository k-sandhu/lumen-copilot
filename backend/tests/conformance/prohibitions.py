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
``app.core.config`` is likewise allowed: reading deployment-level, non-secret
settings is what ``oauth_spec()`` does for the platform's client registration.

**P2 — no mutable module-level state**, detected as *mutation*, not as
container type: a module-level lookup table (``_EXPORT_MIME = {...}``) that is
only ever read is a constant, while ``_cache = {}`` plus a ``.setdefault`` in a
method is cross-run state. The scope analysis is real (:class:`_ScopeWalker`) —
a binding inside a *nested* function does not suppress a genuine mutation in
the enclosing one.

Blind spots, recorded honestly (see the guide's *What this does not catch*):
dynamic imports (``importlib.import_module("app.db…")``, ``__import__``), state
held on a connector *instance* attribute, and mutation reached through an alias
rather than the module-level name. Those stay review-caught; the scan closes
the accident-shaped holes, not a determined author.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

__all__ = [
    "CONNECTOR_ATTR",
    "FORBIDDEN_IMPORTS",
    "Violation",
    "check_execution_context_prohibitions",
    "check_registry_enrollment",
    "connector_package_path",
    "connectors_root",
    "discover_connector_packages",
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


def discover_connector_packages(root: Path) -> list[str]:
    """Every connector-shaped subpackage directory under ``root``.

    Filesystem-first on purpose: this must see a package the *registry* refused
    to enroll, which is exactly the malformed-new-connector case.
    """
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.name.startswith((".", "_"))
        and (entry / "__init__.py").is_file()
    )


def _declares_connector(package: Path) -> bool:
    """Does ``__init__.py`` bind a module-level ``CONNECTOR``? (AST, no import)

    Both real shapes count, because both are how the registry finds it:
    assigning the instance here (``CONNECTOR = GdriveConnector()``) **or**
    re-exporting one built in a submodule (``from .connector import CONNECTOR``,
    which is what the ``web`` connector does).
    """
    tree = ast.parse((package / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == CONNECTOR_ATTR:
                    return True
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == CONNECTOR_ATTR:
                return True
    return False


def check_registry_enrollment(
    root: Path,
    registered: frozenset[str],
    *,
    diagnose: Callable[[str], str | None] | None = None,
) -> None:
    """Every connector package on disk actually enrolls in the registry.

    ``registry._registry()`` **skips** a ``CONNECTOR`` that fails the runtime
    ``Connector`` protocol check rather than raising, so an incomplete new
    connector vanishes from ``registered_types()`` instead of failing. A suite
    parametrized over the registry would then pass green while the connector the
    author just added is not tested at all. This closes that hole from the
    filesystem side — the only side that can see a package the registry dropped.

    ``diagnose`` optionally turns "not enrolled" into the precise reason (which
    protocol method is missing) for a package the caller is able to import.
    """
    packages = discover_connector_packages(root)
    assert packages, (
        f"no connector packages found under {root} — the enrollment scan is looking in "
        "the wrong place, and would pass vacuously"
    )

    problems: list[str] = []
    for package in packages:
        if not _declares_connector(root / package):
            problems.append(
                f"`{package}` has no module-level {CONNECTOR_ATTR} in __init__.py — a "
                "connector registers by drop-in (ADR-0008 §3): "
                f"connectors/{package}/__init__.py must set {CONNECTOR_ATTR} = <impl>()"
            )
            continue
        if package not in registered:
            reason = diagnose(package) if diagnose is not None else None
            problems.append(
                f"`{package}` exposes {CONNECTOR_ATTR} but the registry did not enroll it "
                f"under the name {package!r}. Registry discovery SKIPS a CONNECTOR that "
                "does not satisfy the runtime Connector protocol, so an incomplete "
                "connector silently disappears instead of failing — and every "
                "registry-parametrized conformance test would pass without ever seeing "
                "it. Check that the connector implements name/validate_config/sync/"
                "health and that its `name` equals its package directory name."
                + (f"\n      diagnosis: {reason}" if reason else "")
            )

    assert not problems, (
        "connector packages on disk do not match the registry (see "
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
    """Finds writes to module-level names, honouring lexical scope."""

    def __init__(self, module_names: frozenset[str]) -> None:
        self.module_names = module_names
        self.scopes: list[frozenset[str]] = []
        self.findings: list[tuple[str, int]] = []

    def _shadowed(self, name: str) -> bool:
        return any(name in scope for scope in self.scopes)

    def _is_module_state(self, name: str) -> bool:
        return name in self.module_names and not self._shadowed(name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.scopes.append(_own_scope_bindings(node))
        self.visit(node.body)
        self.scopes.pop()

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        # A comprehension is its own scope in Python 3: its targets shadow there
        # and nowhere else.
        self.scopes.append(_own_scope_bindings(node))
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
        self.scopes.append(_own_scope_bindings(node))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators and defaults evaluate in the ENCLOSING scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.scopes.append(_own_scope_bindings(node))
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
