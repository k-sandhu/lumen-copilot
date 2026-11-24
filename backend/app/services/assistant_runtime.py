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
  and ``source_ids`` become the runtime's narrowing scope — an *additional* filter
  layered on top of the per-user INV-2 permission predicate inside ``retrieval/``,
  never a replacement for it. **Scope may only narrow, never widen** (INV-2): the
  runtime re-filters per the running principal, so an assistant can never surface
  a passage its runner could not already retrieve. The scope's ``modes`` yield the
  **effective modes** (:func:`effective_modes`, #581): an empty (legacy) list means
  ``company`` + ``uploaded``, plus ``web`` when ``web_search`` is allow-listed; a
  non-empty list is authoritative, and ``web_search`` is dropped from the effective
  allow-list unless ``web`` is among them. The ``model`` mode is recorded but never
  honoured — answers stay grounded in retrieved evidence.

This module is pure assembly over a frozen ``config`` dict (the version snapshot)
— no I/O, no adapter — so it is unit-testable in isolation and the runtime change
stays a small, localized thread of two extra parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.entities import AutonomyLevel, KnowledgeMode
from app.services.prompts import GROUNDED_SYSTEM_PROMPT
from app.services.tools.mcp_bridge import is_mcp_tool_name
from app.services.tools.registry import default_allowlist, registered_names


@dataclass(frozen=True, slots=True)
class AssistantRunConfig:
    """The run inputs derived from a pinned assistant version (ADR-0011 §2).

    ``system_prompt`` is the instructions-augmented grounded prompt; ``allowed``
    is the effective allow-list handed to the tool runner (already intersected with
    the registry, and with ``web_search`` removed unless the effective modes carry
    ``web`` — #581); ``collection_ids``/``source_ids`` are the narrowing retrieval
    scope (``None`` ⇒ no narrowing on that axis, exactly like ad-hoc chat).
    ``modes`` are the EFFECTIVE knowledge modes (:func:`effective_modes`).
    ``model`` is the version's frozen model default (``None`` ⇒ the smart server
    default resolved at run time). ``autonomy`` is the version's frozen
    :class:`~app.domain.entities.AutonomyLevel` (issue #218); the caller ``min``'s
    it to the tenant admin cap before handing the EFFECTIVE level to the runner,
    which gates side-effecting T1 tools by it. ``assistant_id`` /
    ``assistant_version_id`` are the run's provenance for the answer audit
    (INV-6); ``None`` when the caller has no pinned version (e.g. a draft test).
    """

    system_prompt: str
    allowed: frozenset[str]
    collection_ids: list[UUID] | None
    model: str | None
    autonomy: AutonomyLevel = AutonomyLevel.SUGGEST
    source_ids: list[UUID] | None = None
    modes: tuple[KnowledgeMode, ...] = ()
    assistant_id: UUID | None = None
    assistant_version_id: UUID | None = None


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


def prepend_user_instructions(custom_instructions: str | None, base_prompt: str) -> str:
    """Prepend a user's custom instructions to an already-composed system prompt.

    The user's per-account preamble goes *first*, before the assistant's
    instructions-augmented grounded prompt (or the bare grounded prompt for ad-hoc
    chat). The final order is therefore ``user custom instructions → (assistant
    instructions if any) → GROUNDED_SYSTEM_PROMPT`` — persona/standing context is read
    first, then the non-negotiable grounding/citation rules (INV-3), which are NEVER
    removed. Blank/absent instructions leave ``base_prompt`` exactly as-is.
    """
    text = (custom_instructions or "").strip()
    if not text:
        return base_prompt
    return f"{text}\n\n{base_prompt}"


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
    return frozenset(name for name in names if is_mcp_tool_name(name) or name in known)


def _scope_uuids(knowledge_scope: object, key: str) -> list[UUID]:
    data = knowledge_scope if isinstance(knowledge_scope, dict) else {}
    out: list[UUID] = []
    for v in _as_list(data.get(key)):
        try:
            out.append(UUID(str(v)))
        except (ValueError, TypeError):
            continue
    return out


def scope_collection_ids(knowledge_scope: object) -> list[UUID] | None:
    """The narrowing collection ids from a frozen scope (``None`` ⇒ no narrowing).

    Reads the version config's ``knowledgeScope.collectionIds`` (camelCase, the
    frozen contract shape). An empty/absent list ⇒ ``None`` so the runtime behaves
    exactly like ad-hoc chat (no collection filter). A malformed id is skipped —
    the scope can only narrow, never widen.
    """
    return _scope_uuids(knowledge_scope, "collectionIds") or None


def scope_source_ids(knowledge_scope: object) -> list[UUID] | None:
    """The narrowing source ids from a frozen scope (``None`` ⇒ no narrowing).

    The ``knowledgeScope.sourceIds`` sibling of :func:`scope_collection_ids`:
    retrieval resolves these to documents in SQL (``documents.source_id``) as an
    additional narrowing over the INV-2 permission predicate (#581).
    """
    return _scope_uuids(knowledge_scope, "sourceIds") or None


def scope_modes(knowledge_scope: object) -> tuple[KnowledgeMode, ...]:
    """The scope's configured modes, de-duplicated in order (unknown skipped)."""
    data = knowledge_scope if isinstance(knowledge_scope, dict) else {}
    out: list[KnowledgeMode] = []
    for v in _as_list(data.get("modes")):
        try:
            mode = KnowledgeMode(str(v))
        except ValueError:
            continue
        if mode not in out:
            out.append(mode)
    return tuple(out)


def effective_modes(knowledge_scope: object, tool_allowlist: object) -> tuple[KnowledgeMode, ...]:
    """The modes a run actually honours (#581 owner decision; shared with scope-ui).

    * **Empty configured modes (legacy versions)** ⇒ ``company`` + ``uploaded``,
      plus ``web`` when ``web_search`` is in the version's tool allow-list — a
      backfill from the tools with no data rewrite, so no published assistant
      silently loses web access it was configured with.
    * **Non-empty configured modes** ⇒ exactly those modes (authoritative):
      ``web_search`` is offered/executable only when ``web`` is among them
      (enforced in :func:`assemble_run_config`).

    The ``model`` mode, when configured, is carried for provenance but never
    honoured — answers remain grounded in retrieved evidence. Reversing this
    decision means changing this one helper.
    """
    configured = scope_modes(knowledge_scope)
    if configured:
        return configured
    modes: list[KnowledgeMode] = [KnowledgeMode.COMPANY, KnowledgeMode.UPLOADED]
    if "web_search" in [str(t) for t in _as_list(tool_allowlist)]:
        modes.append(KnowledgeMode.WEB)
    return tuple(modes)


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


def assemble_run_config(
    config: dict[str, object],
    *,
    assistant_id: UUID | None = None,
    assistant_version_id: UUID | None = None,
) -> AssistantRunConfig:
    """Derive the run inputs from a pinned version's frozen ``config``.

    Pure over the snapshot dict (``AssistantVersionConfig`` camelCase shape) so the
    runtime change is a small, localized thread of the allowed set + the scope + the
    autonomy level, plus the instructions-augmented system prompt. The ``autonomy``
    here is the assistant's OWN configured level; the caller ``min``'s it to the
    tenant cap to get the EFFECTIVE level the runner enforces (issue #218).

    Scope enforcement (#581): the effective modes come from
    :func:`effective_modes`, and ``web_search`` is removed from the effective
    allow-list unless ``web`` is among them — here, at assembly, so every run
    path (chat, headless, test) inherits the same decision and the runner's
    allow-list chokepoint does the rest. ``assistant_id`` /
    ``assistant_version_id`` ride along purely as audit provenance.
    """
    model = config.get("model")
    scope = config.get("knowledgeScope")
    modes = effective_modes(scope, config.get("toolAllowlist"))
    allowed = resolve_allowlist(config.get("toolAllowlist"))
    if KnowledgeMode.WEB not in modes:
        allowed = allowed - {"web_search"}
    return AssistantRunConfig(
        system_prompt=build_system_prompt(_as_str(config.get("instructions"))),
        allowed=allowed,
        collection_ids=scope_collection_ids(scope),
        model=str(model) if isinstance(model, str) and model else None,
        autonomy=resolve_autonomy(config.get("autonomyLevel")),
        source_ids=scope_source_ids(scope),
        modes=modes,
        assistant_id=assistant_id,
        assistant_version_id=assistant_version_id,
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "AssistantRunConfig",
    "assemble_run_config",
    "build_system_prompt",
    "effective_modes",
    "prepend_user_instructions",
    "resolve_allowlist",
    "resolve_autonomy",
    "scope_collection_ids",
    "scope_modes",
    "scope_source_ids",
]
