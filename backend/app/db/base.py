"""Declarative base for all ORM models.

Models (added as features land) subclass :class:`Base`; Alembic's ``env.py``
imports ``Base.metadata`` as its autogenerate target. Vector columns and their
ACL/metadata live in the same row (ADR-0003 §3) so permission-aware retrieval
is a ``WHERE`` clause — but no such models exist in this skeleton yet.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base. All ORM models inherit from this."""
