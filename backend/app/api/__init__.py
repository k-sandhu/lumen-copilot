"""HTTP API layer — routers and FastAPI dependencies.

Single responsibility (ADR-0004 layering): routers parse/validate the request,
call **one** service, and shape the response. **No** DB access, external I/O, or
business logic here. Feature routes are versioned under ``api/v1`` and mounted
at ``/api/v1``; the health probes are unversioned (``/health``,
``/health/ready``) per ``contracts/openapi.yaml``.
"""
