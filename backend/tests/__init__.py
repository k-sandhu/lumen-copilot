"""Backend test suite.

Mirrors ``app/``: ``domain``/``services`` unit tests (pure, no mocks), ``api``
integration tests against the ``contracts/`` shapes. External dependencies are
mocked here so the unit/API layer runs without a live stack (live tests against
compose are a separate tier — AGENTS.md §10).
"""
