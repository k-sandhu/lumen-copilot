"""Lumen Copilot backend application package.

A multi-tenant, grounded chat assistant backend (FastAPI, async). Layering is
one-directional — ``api/ -> services/ -> domain/`` with adapters hung off
services — and each external system has exactly one owning adapter module
(ADR-0004). See ``backend/AGENTS.md`` for the binding coding contract.
"""

__version__ = "0.0.1"
