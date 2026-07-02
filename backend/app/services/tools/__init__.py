"""The governed tool platform (CC-7 #207) — the action/tool-call gateway.

The seam every agent tool plugs into: a tool declares a JSON-Schema + **risk tier**
+ approval requirement (:mod:`app.services.tools.types`), self-registers by
dropping a file in :mod:`app.services.tools.impls` (:mod:`app.services.tools.registry`),
and every invocation flows through **one** governed path
(:mod:`app.services.tools.runner`): allow-list check → approval seam → bounded
execute → uniform :class:`~app.domain.tools.ToolResult` → ``tool_invocations`` row
+ ``tool.invoked``/``tool.result`` audit. A denied or failing tool becomes a
*result*, never a crashed run (issue #207 §7).

Layering (ADR-0004): this is a ``services/`` concern — it composes the ``auth``
principal, the ``retrieval/`` adapter, the ``db/`` ``tool_invocations`` repository,
and the audit sink. The pure vocabulary (risk tier, result, error codes) lives in
:mod:`app.domain.tools`; nothing here leaks a vendor/adapter type upward.

**No re-export wall (ADR-0008 §3):** import from the concrete module
(``from app.services.tools.runner import ToolRunner``) so adding a tool touches
only its own file under ``impls/``, never this barrel. This file is docstring-only.
"""
