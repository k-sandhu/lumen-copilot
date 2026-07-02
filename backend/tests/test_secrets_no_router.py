"""AC-3 (architecture/contract) — no router can return a secret value (issue #209).

The vault's central guarantee: **there is no endpoint that returns a secret
value**. The MCP / search adapters fetch plaintext *in-process* through
``SecretsService.get_secret_plaintext``; any HTTP surface is write + list-metadata
+ delete only. Rather than assert "some endpoint doesn't leak", these prove the
stronger structural invariant that makes a leak *impossible to wire by accident*:

1. The **cipher** (``app.core.crypto``) is imported by exactly one non-test
   module — ``app.services.secrets_service`` — so nothing else can decrypt.
2. **No module under ``app.api``** imports the secrets service or the cipher, and
   none references ``get_secret_plaintext`` — so no route can reach a plaintext.
3. The public secret **wire shape** (``SecretRef``) has no field that could carry
   a value/ciphertext/nonce, so even a route that *did* serialize one leaks
   nothing.

This is the "architecture test" AC-3 calls for; it fails closed if a future change
wires the plaintext-getter (or the cipher) into an ``api/`` module.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from app.domain.entities import SecretRef

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_APP_ROOT = _BACKEND_ROOT / "app"


def _module_name(path: Path) -> str:
    """Dotted module name for an app/ file (e.g. app/api/v1/foo.py → app.api.v1.foo)."""
    rel = path.relative_to(_BACKEND_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _imports_module(path: Path, target: str) -> bool:
    """Whether ``path``'s source imports ``target`` (import X / from X import …).

    A static AST scan (no import side effects): matches ``import target`` and
    ``from target import …`` including submodules (``target.sub``).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target or alias.name.startswith(f"{target}."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == target or mod.startswith(f"{target}."):
                return True
    return False


def _references_name(path: Path, name: str) -> bool:
    """Whether ``path``'s source references the identifier ``name`` anywhere."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
    return False


def _app_py_files() -> list[Path]:
    return [p for p in _APP_ROOT.rglob("*.py") if p.is_file()]


def _api_py_files() -> list[Path]:
    return [p for p in (_APP_ROOT / "api").rglob("*.py") if p.is_file()]


def test_cipher_is_imported_only_by_the_secrets_service() -> None:
    """The cipher (``app.core.crypto``) has exactly one non-test importer.

    ADR-0004 chokepoint: envelope encryption lives in one module and only the
    write-only service imports it. ``core.config`` references the crypto *loader*
    for key validation via a function-local import (fail-fast), which is allowed —
    it never decrypts; the assertion is that no module *other than* the secrets
    service and config imports the cipher.
    """
    allowed = {"app.services.secrets_service", "app.core.config", "app.core.crypto"}
    importers = {
        _module_name(p) for p in _app_py_files() if _imports_module(p, "app.core.crypto")
    }
    unexpected = importers - allowed
    assert unexpected == set(), (
        "app.core.crypto (the cipher) must be imported only by the secrets service "
        f"(+ config's key validation); unexpected importers: {sorted(unexpected)}"
    )


def test_no_api_module_imports_the_secrets_service_or_cipher() -> None:
    """No ``app.api`` module imports the secrets service or the cipher (AC-3).

    If no router can even import the service or the cipher, no route can return a
    secret value or decrypt one. This is the structural form of "write + list +
    delete only".
    """
    offenders = [
        _module_name(p)
        for p in _api_py_files()
        if _imports_module(p, "app.services.secrets_service")
        or _imports_module(p, "app.core.crypto")
    ]
    assert offenders == [], (
        "an app.api module imports the secrets service/cipher — a route could then "
        f"return a secret value (AC-3 violation): {offenders}"
    )


def test_no_api_module_references_get_secret_plaintext() -> None:
    """No ``app.api`` module names ``get_secret_plaintext`` — the internal-only getter.

    Belt-and-braces alongside the import check: the plaintext getter is in-process
    only (adapters call it), never reachable from a router.
    """
    offenders = [
        _module_name(p) for p in _api_py_files() if _references_name(p, "get_secret_plaintext")
    ]
    assert offenders == [], (
        f"an app.api module references get_secret_plaintext (internal-only): {offenders}"
    )


def test_secret_ref_wire_shape_carries_no_value() -> None:
    """The public secret shape (``SecretRef``) has no value/ciphertext/nonce field.

    Even a hypothetical endpoint that serialized a ``SecretRef`` would leak
    nothing: the only fields are identity + metadata + the masked hint.
    """
    fields = {f.name for f in dataclasses.fields(SecretRef)}
    forbidden = {"ciphertext", "nonce", "plaintext", "value", "secret"}
    assert fields & forbidden == set(), f"SecretRef exposes a value-bearing field: {fields}"
    assert fields == {
        "id",
        "tenant_id",
        "owner_id",
        "name",
        "kind",
        "hint",
        "created_at",
        "updated_at",
    }
