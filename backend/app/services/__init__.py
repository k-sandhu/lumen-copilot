"""Services layer — use-cases / orchestration.

Single responsibility (ADR-0004 layering): the only layer that *composes*
adapters (``db``, ``llm``, ``retrieval``, ``storage``, ``tasks``, ``realtime``,
``auth``) to fulfil a use-case. Holds *what the app does*. Routers call exactly
one service and shape its result; services hold no HTTP concerns and return
domain types. Dependencies point inward (``api -> services -> domain``); a
service never imports a router. No use-cases exist yet — this is the reserved
seam where feature orchestration lands.
"""
