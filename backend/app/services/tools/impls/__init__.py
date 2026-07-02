"""Tool implementations — auto-discovered by the registry (CC-7 #207 §1).

Each module here exposes a module-level ``TOOLS`` sequence of
:class:`~app.services.tools.types.ToolDefinition`; the registry
(:mod:`app.services.tools.registry`) scans this package and collects them. Adding a
tool is a **new file** in this package — no edit to the registry or any include
list (ADR-0008 §3 auto-discovery). The MVP ships :mod:`retrieval` (the three
read-only retrieval tools migrated behind the registry); web-search / file-write /
``run_python`` / MCP tools land here later, each as its own file + issue.
"""
