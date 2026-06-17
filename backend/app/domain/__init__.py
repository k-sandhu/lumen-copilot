"""Domain layer: entities, value objects, pure policy.

Single responsibility: the business vocabulary of Lumen Copilot, expressed as
plain types and pure functions. **No I/O and no framework imports** (no
FastAPI, no SQLAlchemy, no LiteLLM) — this layer depends on nothing and is
unit-testable with zero mocks (ADR-0004). Adapters translate vendor types into
these domain types at the boundary, so the rest of the app never sees a vendor
object.
"""
