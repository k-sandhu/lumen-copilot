"""The ADR-0019 §4 execution-context prohibitions, pinned structurally.

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
function-local imports (which is exactly how a connector would smuggle one in).

Two rules:

**P1 — forbidden imports** (:data:`FORBIDDEN_IMPORTS`): the vault, the DB (and
the DB toolkit one layer down, so opening a private engine is not a bypass),
and the object store. ``app.core.config`` is deliberately **allowed** — reading
deployment-level, non-secret settings is what ``oauth_spec()`` does for the
platform's client registration (ADR-0019 §1).

**P2 — no mutable module-level state**, detected as *mutation*, not as
container type: a module-level lookup table (``_EXPORT_MIME = {...}``) that is
only ever read is a constant, while ``_cache = {}`` plus a ``.setdefault`` in a
method is cross-run state. Flagged: a ``global`` statement, a mutating method
call / item assignment / attribute assignment against a module-level name, and
a module-level mutable literal bound to a non-constant-cased name (the shape a
cache is always written in).

Blind spots, recorded honestly (see the guide's *What this does not catch*):
dynamic imports (``importlib.import_module("app.db…")``, ``__import__``),
state held on a connector *instance* attribute, and mutation reached through an
alias rather than the module-level name. Those stay review-caught; the scan
closes the accident-shaped holes, not a determined author.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

__all__ = [
    "FORBIDDEN_IMPORTS",
    "Violation",
    "check_execution_context_prohibitions",
    "connector_package_path",
    "scan_package",
]


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
        "the database (session / models / repositories) — connectors never read "
        "or write the DB; return state to the framework as FetchedDoc / "
        "SyncPage fields and let its per-page transaction persist it"
    ),
    "sqlalchemy": (
        "the DB toolkit itself — a private engine inside a connector is the "
        "app.db prohibition one layer down, not a way around it"
    ),
    "app.storage": (
        "the object store — persisting content is the framework's job; a "
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


@dataclass(frozen=True, slots=True)
class Violation:
    """One prohibition breach, located precisely enough to fix."""

    rule: str
    module: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} [{self.rule}] {self.detail}"


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


def _module_name(package: Path, path: Path) -> str:
    rel = path.relative_to(package).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join([f"app.connectors.{package.name}", *parts])


def _iter_python_files(package: Path) -> Iterator[Path]:
    yield from sorted(p for p in package.rglob("*.py") if p.is_file())


def _imported_modules(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """Every absolute module name an import could name — module level or inside a
    function body (a deferred import hides nothing from an AST walk).

    ``from X import y`` yields **both** ``X`` and ``X.y``: the forbidden module
    is just as often the imported *name* (``from app.services import
    secrets_service``) as the module path, and a lazy in-function import is
    exactly the shape that takes.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno
            for alias in node.names:
                if alias.name != "*":
                    yield f"{node.module}.{alias.name}", node.lineno


def _forbidden_import(module: str) -> tuple[str, str] | None:
    for prefix, reason in FORBIDDEN_IMPORTS.items():
        if module == prefix or module.startswith(prefix + "."):
            return prefix, reason
    return None


def _module_level_names(tree: ast.Module) -> frozenset[str]:
    """Names bound by a top-level assignment (the candidate module state)."""
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
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


def _local_bindings(func: ast.AST) -> frozenset[str]:
    """Names a function binds locally (so a same-named local never looks like
    module state). Deliberately generous — a false *local* only ever makes the
    scan quieter, and the mutable-literal rule already catches the shape."""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return frozenset(names)


def _mutations_of_module_state(
    tree: ast.Module, module_names: frozenset[str]
) -> Iterator[tuple[str, int]]:
    """Writes to a module-level name from inside a function body."""
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        shadowed = _local_bindings(func)
        for node in ast.walk(func):
            if isinstance(node, ast.Global):
                for name in node.names:
                    yield f"`global {name}` rebinds module state", node.lineno
                continue
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.Delete):
                targets = list(node.targets)
            for target in targets:
                base = _base_name(target)
                if base is not None and base in module_names and base not in shadowed:
                    yield f"writes to module-level `{base}`", node.lineno
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATING_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_names
                and node.func.value.id not in shadowed
            ):
                yield (
                    f"`{node.func.value.id}.{node.func.attr}(...)` mutates module state",
                    node.lineno,
                )


def _base_name(target: ast.expr) -> str | None:
    """The module-level name a subscript/attribute write ultimately lands on."""
    if isinstance(target, ast.Subscript | ast.Attribute):
        inner = target.value
        return inner.id if isinstance(inner, ast.Name) else None
    return None


def scan_package(package: Path) -> list[Violation]:
    """Every prohibition breach in a connector package (empty = conformant)."""
    violations: list[Violation] = []
    for path in _iter_python_files(package):
        module = _module_name(package, path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported, line in _imported_modules(tree):
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
        module_names = _module_level_names(tree)
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
        for detail, line in _mutations_of_module_state(tree, module_names):
            violations.append(
                Violation(rule="no-mutable-module-state", module=module, line=line, detail=detail)
            )
    return violations


def check_execution_context_prohibitions(package: Path) -> None:
    """Assert a connector package obeys the §4 execution-context prohibitions."""
    violations = scan_package(package)
    assert not violations, (
        f"connector package `{package.name}` breaks the ADR-0019 §4 execution-context "
        "prohibitions (connectors never touch the vault, the DB, or mutable module "
        "state — see docs/guides/building-a-connector.md):\n  "
        + "\n  ".join(str(v) for v in violations)
    )
