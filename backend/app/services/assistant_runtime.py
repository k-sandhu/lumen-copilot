"""Assistant → run-config assembler (ADR-0011 §2, issue #211).

The thin ``services/`` seam that turns a pinned assistant version's frozen config
into the three inputs the chat runtime already consumes — **without** a new
runtime (ADR-0011 §2). It lives here, not in ``chat_runtime`` (which stays engine)
nor in ``api/`` (which stays thin):

* ``instructions`` → the **system prompt**. The version's instructions *prepend*
  to ``GROUNDED_SYSTEM_PROMPT`` so persona/role is added while the grounding /
  citation contract (INV-3) is preserved — instructions never remove the grounding
  rules.
* ``tool_allowlist`` → the **allowed-tool set**. Intersected with the live CC-A
  registry (an entry that has since left the registry is silently dropped —
  deny-by-default); ``[]`` ⇒ the ad-hoc default set (the read-only retrieval
  tools). The runner enforces this set as the hard chokepoint.
* ``knowledge_scope`` → the **retrieval filter**. The scope's ``collection_ids``
  become the runtime's ``collection_ids`` — an *additional narrowing* filter layered
  on top of the per-user INV-2 permission predicate inside ``retrieval/``, never a
  replacement for it. **Scope may only narrow, never widen** (INV-2): the runtime
  re-filters per the running principal, so an assistant can never surface a passage
  its runner could not already retrieve.

This module is pure assembly over a frozen ``config`` dict (the version snapshot)
— no I/O, no adapter — so it is unit-testable in isolation and the runtime change
stays a small, localized thread of two extra parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.entities import AutonomyLevel
from app.services.prompts import GROUNDED_SYSTEM_PROMPT
from app.services.tools.mcp_bridge import is_mcp_tool_name
from app.services.tools.registry import default_allowlist, registered_names


@dataclass(frozen=True, slots=True)
class AssistantRunConfig:
    """The run inputs derived from a pinned assistant version (ADR-0011 §2).

    ``system_prompt`` is the instructions-augmented grounded prompt; ``allowed``
    is the effective allow-list handed to the tool runner (already intersected with
    the registry); ``collection_ids`` is the narrowing retrieval scope (``None`` ⇒
    no collection narrowing, exactly like ad-hoc chat). ``model`` is the version's
    frozen model default (``None`` ⇒ the smart server default resolved at run time).
    ``autonomy`` is the version's frozen :class:`~app.domain.entities.AutonomyLevel`
    (issue #218); the caller ``min``'s it to the tenant admin cap before handing the
    EFFECTIVE level to the runner, which gates side-effecting T1 tools by it.
    """

    system_prompt: str
    allowed: frozenset[str]
    collection_ids: list[UUID] | None
    model: str | None
    autonomy: AutonomyLevel = AutonomyLevel.SUGGEST


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def build_system_prompt(instructions: str | None) -> str:
    """Prepend an assistant's instructions to the grounded system prompt.

    Persona/role is added *before* the grounding contract so the model reads the
    assistant's character first, then the non-negotiable grounding/citation rules
    (INV-3) — the instructions can shape tone/scope but never remove grounding.
    Blank/absent instructions leave the grounded prompt exactly as ad-hoc chat.
    """
    text = (instructions or "").strip()
    if not text:
        return GROUNDED_SYSTEM_PROMPT
    return f"{text}\n\n{GROUNDED_SYSTEM_PROMPT}"


def resolve_allowlist(tool_allowlist: object) -> frozenset[str]:
    """The effective allowed-tool set for a run given the assistant's allow-list.

    ``[]`` (or a missing list) ⇒ the ad-hoc default set (the read-only retrieval
    tools). A non-empty list keeps:

    * a **native** name only if it is still in the live registry (an entry removed
      since the draft was saved is dropped — deny-by-default, issue #207);
    * an **``mcp:*``** name as-is (issue #227). MCP tools are tenant-scoped and
      dynamic, so they are NOT in the static registry; whether a named MCP tool is
      actually offered/invokable is decided per-run by the runtime resolver (a
      disabled / unregistered / cross-tenant server contributes nothing) and the
      runner's hard allow-list chokepoint — never here.

    The runner still enforces this set as the hard chokepoint; this only decides
    what to offer.
    """
    names = [str(t) for t in _as_list(tool_allowlist)]
    if not names:
        return default_allowlist()
    known = registered_names()
    return frozenset(
        name for name in names if is_mcp_tool_name(name) or name in known
    )


def scope_collection_ids(knowledge_scope: object) -> list[UUID] | None:
    """The narrowing collection ids from a frozen scope (``None`` ⇒ no narrowing).

    Reads the version config's ``knowledgeScope.collectionIds`` (camelCase, the
    frozen contract shape). An empty/absent list ⇒ ``None`` so the runtime behaves
    exactly like ad-hoc chat (no collection filter). A malformed id is skipped —
    the scope can only narrow, never widen.
    """
    data = knowledge_scope if isinstance(knowledge_scope, dict) else {}
    out: list[UUID] = []
    for v in _as_list(data.get("collectionIds")):
        try:
            out.append(UUID(str(v)))
        except (ValueError, TypeError):
            continue
    return out or None


def resolve_autonomy(raw: object) -> AutonomyLevel:
    """The assistant's frozen autonomy level from the version config (default suggest).

    Reads the version config's ``autonomyLevel`` (the frozen contract value). An
    unknown / absent value falls back to ``SUGGEST`` — the safest default (a T1 side
    effect stays gated until the config explicitly raises autonomy). The tenant admin
    cap is applied by the caller (``min(this, cap)``), not here — this is the
    assistant's own configured level.
    """
    try:
        return AutonomyLevel(str(raw))
    except ValueError:
        return AutonomyLevel.SUGGEST


def assemble_run_config(config: dict[str, object]) -> AssistantRunConfig:
    """Derive the run inputs from a pinned version's frozen ``config``.

    Pure over the snapshot dict (``AssistantVersionConfig`` camelCase shape) so the
    runtime change is a small, localized thread of the allowed set + the scope + the
    autonomy level, plus the instructions-augmented system prompt. The ``autonomy``
    here is the assistant's OWN configured level; the caller ``min``'s it to the
    tenant cap to get the EFFECTIVE level the runner enforces (issue #218).
    """
    model = config.get("model")
    return AssistantRunConfig(
        system_prompt=build_system_prompt(_as_str(config.get("instructions"))),
        allowed=resolve_allowlist(config.get("toolAllowlist")),
        collection_ids=scope_collection_ids(config.get("knowledgeScope")),
        model=str(model) if isinstance(model, str) and model else None,
        autonomy=resolve_autonomy(config.get("autonomyLevel")),
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "AssistantRunConfig",
    "assemble_run_config",
    "build_system_prompt",
    "resolve_allowlist",
    "resolve_autonomy",
    "scope_collection_ids",
]
