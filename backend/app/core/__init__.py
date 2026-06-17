"""Cross-cutting plumbing: config (the only env reader), logging, errors.

Single responsibility: process-wide concerns that every layer leans on but
that are not themselves a domain or an external adapter — settings
(``pydantic-settings``), structured logging, and the typed error hierarchy +
problem-detail mapper. ``config.py`` is the *only* reader of ``os.environ``
(ADR-0004 boundary table).
"""
