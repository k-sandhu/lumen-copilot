"""Conversational agent builder — draft an assistant from a description (E6-1, #213).

The ``services/`` seam behind ``POST /assistants/draft``: it turns a plain-language
description into a **draft** ``AssistantVersionConfig`` the F-AB-2 editor can load
for review — **nothing is created** (the user saves via ``POST /assistants``). It
composes the ``llm/`` gateway (a structured prompt → a parsed JSON draft) with the
same governance the CRUD service enforces, but *softly*: a drafting step never 422s
on a bad tool/scope guess — it **omits** the invalid field and returns a human
**note**, and asks a **clarifying question** for anything the description left
ambiguous (missing scope / ownership / risk acknowledgement).

**Boundary (ADR-0004).** This service is the only orchestration point: the router
validates in → calls this one service → shapes out. The model is reached only
through the ``llm.LLMGateway`` adapter (never ``litellm``); the tool vocabulary
comes from the CC-A registry (``services.tools.registry``); the scope ids are
checked against the caller's own collections/sources (tenant-scoped repositories).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).** The
draft can only reference tools that exist and collections/sources the *caller*
owns; a name the model invents that the caller cannot see is dropped (never
surfaced as existing), so the builder can never leak a foreign resource. The
``tenant_id``/``owner_id`` come from the resolved principal, never request input.

**Audited (INV-6).** Every draft emits ``assistant.drafted`` — the intent to draft
is provable (who asked, how many clarifications/omissions), even though nothing is
persisted.

**Fail-soft, never fail-open.** If the model is unconfigured/unavailable, or its
output is unparseable, the service returns a **minimal, safe** draft (the
description as instructions, empty scope/allow-list, ``suggest`` autonomy) plus a
clarification asking the user to fill the gaps by hand — the builder degrades to a
pre-filled-but-empty editor rather than crashing or inventing state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.repositories import CollectionRepository, SourceRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, AutonomyLevel
from app.domain.llm import ChatMessage, Role
from app.llm import LLMGateway
from app.services.audit import AuditSink
from app.services.tools.mcp_bridge import is_mcp_tool_name
from app.services.tools.registry import all_tools, registered_names

_MAX_DESCRIPTION = 4000
_MAX_LIST = 200
# How much of the model's answer to keep when parsing — a defensive cap so a
# runaway completion cannot balloon memory (the draft is a small JSON object).
_MAX_COMPLETION = 20_000

_ALLOWED_AUTONOMY = {level.value for level in AutonomyLevel}


@dataclass(frozen=True, slots=True)
class DraftConfig:
    """The drafted assistant config the FE loads into the editor (contract shape).

    Mirrors ``AssistantVersionConfig`` (camelCase on the wire) minus the immutable
    version envelope: exactly the fields the editor pre-fills. ``model`` ``None`` ⇒
    the smart server default; ``toolAllowlist``/scope default empty (the safe,
    deny-by-default draft). Nothing here is persisted — it is a *suggestion*.
    """

    name: str
    description: str | None
    instructions: str | None
    model: str | None
    collection_ids: tuple[UUID, ...]
    source_ids: tuple[UUID, ...]
    tool_allowlist: tuple[str, ...]
    autonomy_level: AutonomyLevel


@dataclass(frozen=True, slots=True)
class DraftResult:
    """A drafted config + the builder's clarifying questions and omission notes.

    ``clarifications`` are the questions the builder asks for anything the
    description left ambiguous (missing scope / owner / risk acknowledgement,
    E6-3) — the FE surfaces them so the user answers *before* saving.
    ``notes`` explain any field the model proposed that was **omitted** because it
    referenced an unknown tool or an unseeable collection/source (the transparency
    trail for the deny-by-default drop).
    """

    draft: DraftConfig
    clarifications: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class AssistantDraftService:
    """Draft an assistant config from a plain-language description (E6-1).

    Constructed per-request with the session, the resolved ``tenant_id`` /
    ``owner_id`` (from the token, never request input), the LLM gateway adapter, and
    the audit sink + correlation context. All governance (tool/scope validation,
    the high-risk warning, the clarifying-question policy, audit) lives here; the
    router only (de)serialises.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        llm: LLMGateway,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._collections = CollectionRepository(session, tenant_id)
        self._sources = SourceRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._llm = llm
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    async def draft(self, *, description: str) -> DraftResult:
        """Draft a config for ``description`` — validate softly, never persist.

        Runs the structured builder prompt through the gateway, parses the JSON
        draft, then:

        * validates every proposed tool name against the CC-A registry — an unknown
          name is **omitted** with a ``note`` (deny-by-default; never a 422 here);
        * validates every proposed collection/source id against the caller's own
          resources — an id the caller cannot see is **omitted** with a ``note``
          (INV-2; a foreign/unknown resource is never surfaced);
        * asks a **clarifying question** for a missing knowledge scope, a missing
          owner/backup, and (when a high-risk tool is drafted) a risk
          acknowledgement (E6-3);
        * **warns** when a drafted tool is high-risk (write-tier / requires
          approval) so the FE can gate publish behind an explicit acknowledgement.

        Emits ``assistant.drafted`` (INV-6). A model failure/unparseable output
        degrades to a minimal safe draft + a clarification (fail-soft).
        """
        text = description.strip()
        raw = await self._complete(text)
        parsed = _parse_draft(raw)

        # --- name / description / instructions (free text, trimmed) ---
        name = _trimmed(parsed.get("name")) or _fallback_name(text)
        draft_description = _trimmed(parsed.get("description")) or None
        instructions = _trimmed(parsed.get("instructions")) or (text or None)
        model = _trimmed(parsed.get("model")) or None
        autonomy = _autonomy(parsed.get("autonomyLevel"))

        notes: list[str] = []
        clarifications: list[str] = list(_clarifications(parsed))
        warnings: list[str] = []

        # --- tool allow-list: keep only real, registered tools (deny-by-default) ---
        allowlist, dropped_tools = self._resolve_tools(parsed.get("toolAllowlist"))
        if dropped_tools:
            notes.append(
                "Omitted tool(s) the description referenced but that are not available: "
                f"{', '.join(dropped_tools)}."
            )
        warnings.extend(_high_risk_warnings(allowlist))

        # --- knowledge scope: keep only ids the caller can see (INV-2) ---
        collection_ids, dropped_collections = await self._resolve_collections(
            parsed.get("collectionIds")
        )
        source_ids, dropped_sources = await self._resolve_sources(parsed.get("sourceIds"))
        if dropped_collections:
            notes.append(
                "Omitted collection(s) you cannot access "
                f"({len(dropped_collections)}); pick from your own collections instead."
            )
        if dropped_sources:
            notes.append(
                "Omitted source(s) you cannot access "
                f"({len(dropped_sources)}); pick from your own sources instead."
            )

        # --- clarifying questions the builder always asks for missing governance ---
        if not collection_ids and not source_ids:
            clarifications.append(
                "Which collections or sources should this assistant read from? "
                "Its knowledge scope is empty."
            )
        clarifications.append(
            "Who is the accountable owner, and who is the backup owner? "
            "Both are required before you can publish."
        )
        if warnings:
            clarifications.append(
                "This draft uses a tool that can take consequential actions. "
                "Do you acknowledge the risk and want to keep it?"
            )

        result = DraftResult(
            draft=DraftConfig(
                name=name,
                description=draft_description,
                instructions=instructions,
                model=model,
                collection_ids=collection_ids,
                source_ids=source_ids,
                tool_allowlist=allowlist,
                autonomy_level=autonomy,
            ),
            clarifications=_dedupe(clarifications),
            notes=tuple(notes),
            warnings=tuple(warnings),
        )
        await self._emit_audit(text, result)
        return result

    # --- model call -----------------------------------------------------------

    async def _complete(self, description: str) -> str:
        """Ask the model for a JSON draft; return ``""`` on any failure (fail-soft).

        The gateway maps a provider fault to a typed :class:`AppError`; we swallow
        it here and return an empty string so :meth:`draft` degrades to the minimal
        safe draft rather than surfacing a 503 for a builder convenience. The
        description is passed as the user turn; the builder rules are the system
        turn.
        """
        if not self._llm.enabled:
            return ""
        messages = (
            ChatMessage(role=Role.SYSTEM, content=_BUILDER_SYSTEM_PROMPT),
            ChatMessage(role=Role.USER, content=description[:_MAX_DESCRIPTION]),
        )
        try:
            completion = await self._llm.chat(messages)
        except AppError:
            return ""
        return completion.content[:_MAX_COMPLETION]

    # --- validation -----------------------------------------------------------

    def _resolve_tools(self, raw: object) -> tuple[tuple[str, ...], list[str]]:
        """Split proposed tool names into (kept registered natives, dropped unknowns).

        Deny-by-default: a native name is kept only if it is in the live CC-A
        registry; an ``mcp:*`` name the model invents is dropped here (the builder
        cannot validate a caller's MCP tools without their server list, and the CRUD
        service re-validates on save anyway — a drop is the safe default). Order and
        uniqueness are preserved for a stable draft.
        """
        names = _str_list(raw)
        known = registered_names()
        kept: list[str] = []
        dropped: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            if not is_mcp_tool_name(name) and name in known:
                kept.append(name)
            else:
                dropped.append(name)
        return tuple(kept), dropped

    async def _resolve_collections(self, raw: object) -> tuple[tuple[UUID, ...], list[str]]:
        """Keep only collection ids the caller owns; return (kept, dropped-as-text)."""
        owned = {c.id for c in await self._collections.list_for_owner(self._owner_id)}
        return _partition_ids(raw, owned)

    async def _resolve_sources(self, raw: object) -> tuple[tuple[UUID, ...], list[str]]:
        """Keep only source ids the caller owns; return (kept, dropped-as-text)."""
        sources = await self._sources.list_for_owner_page(self._owner_id, limit=_MAX_LIST)
        owned = {s.id for s in sources}
        return _partition_ids(raw, owned)

    # --- audit ----------------------------------------------------------------

    async def _emit_audit(self, description: str, result: DraftResult) -> None:
        """Emit ``assistant.drafted`` (INV-6) — records intent, never the raw text.

        The description can carry sensitive text, so only its length + the shape of
        the draft (clarification/omission/warning counts, autonomy, tool count) is
        recorded — enough to prove *who asked the builder to draft what* without
        persisting the prompt.
        """
        await self._audit.emit(
            action=AuditAction.ASSISTANT_DRAFTED,
            actor=AuditActor.user(self._owner_id),
            resource_type="assistant",
            resource_id="draft",
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "description_length": len(description),
                "tool_count": len(result.draft.tool_allowlist),
                "clarification_count": len(result.clarifications),
                "note_count": len(result.notes),
                "warning_count": len(result.warnings),
                "autonomy_level": result.draft.autonomy_level.value,
            },
        )


# --- module-level helpers ----------------------------------------------------


_BUILDER_SYSTEM_PROMPT = (
    "You are the Lumen assistant builder. Turn the user's plain-language "
    "description of an assistant into a single JSON object describing its "
    "configuration. Respond with ONLY the JSON object, no prose, no code fence.\n\n"
    "The object has these keys (all optional; omit what the description does not "
    "specify — do NOT invent values):\n"
    '  "name": a short human name for the assistant;\n'
    '  "description": a one-line summary;\n'
    '  "instructions": the system-prompt persona/role text;\n'
    '  "model": a model id, only if the user named one;\n'
    '  "collectionIds": UUIDs of collections it should read from, only if the '
    "user gave ids;\n"
    '  "sourceIds": UUIDs of connected sources, only if the user gave ids;\n'
    '  "toolAllowlist": names of tools it may use, from the tool catalogue below;\n'
    '  "autonomyLevel": one of "suggest", "draft", "act_with_approval", "act_auto";\n'
    '  "clarifications": a list of short questions to ask the user for anything '
    "the description left ambiguous (missing scope, owner, or risk).\n\n"
    "Only use tool names from this catalogue: " + ", ".join(sorted(registered_names())) + ".\n"
    "Never invent collection or source ids. Prefer the least autonomy that fits."
)


def _fallback_name(description: str) -> str:
    """A safe default name when the model gave none (first few words of the ask)."""
    words = description.split()
    if not words:
        return "New assistant"
    return " ".join(words[:6])[:200] or "New assistant"


def _parse_draft(raw: str) -> dict[str, object]:
    """Parse the model's answer into a dict, defensively (``{}`` on any failure).

    The model is asked for a bare JSON object; real providers sometimes wrap it in
    a code fence or add a stray prefix, so we extract the outermost ``{...}`` span
    before parsing. A non-object or unparseable answer yields ``{}`` — the caller
    then builds the minimal safe draft.
    """
    text = raw.strip()
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _trimmed(value: object) -> str:
    """A trimmed string for a scalar value; ``""`` for anything non-stringable/blank."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _autonomy(value: object) -> AutonomyLevel:
    """The proposed autonomy level, or the safe ``SUGGEST`` default (deny-by-default)."""
    raw = value if isinstance(value, str) else ""
    if raw in _ALLOWED_AUTONOMY:
        return AutonomyLevel(raw)
    return AutonomyLevel.SUGGEST


def _str_list(raw: object) -> list[str]:
    """Coerce a value into a trimmed, non-empty string list (bounded)."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:_MAX_LIST]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _clarifications(parsed: dict[str, object]) -> list[str]:
    """The model-proposed clarifying questions (trimmed, bounded)."""
    return _str_list(parsed.get("clarifications"))


def _partition_ids(raw: object, owned: set[UUID]) -> tuple[tuple[UUID, ...], list[str]]:
    """Split proposed ids into (kept ⊆ owned, dropped-as-text). Malformed → dropped.

    Preserves order and de-duplicates. A syntactically-invalid id or one the caller
    does not own is dropped and returned as its original text for the omission note
    (never surfaced as a real scope entry — INV-2).
    """
    kept: list[UUID] = []
    dropped: list[str] = []
    seen: set[UUID] = set()
    for token in _str_list(raw):
        try:
            candidate = UUID(token)
        except (ValueError, TypeError):
            dropped.append(token)
            continue
        if candidate in owned and candidate not in seen:
            seen.add(candidate)
            kept.append(candidate)
        elif candidate not in owned:
            dropped.append(token)
    return tuple(kept), dropped


def _high_risk_warnings(allowlist: tuple[str, ...]) -> list[str]:
    """A warning per drafted tool that can take consequential actions.

    A tool is high-risk when it is **not read-only** (a write-tier T1+ tool) or it
    ``requires_approval`` — the model should never be handed one silently. The
    warning names the tool so the FE can gate publish behind an explicit
    acknowledgement (AC-3 / CC-A tiers).
    """
    by_name = {t.name: t for t in all_tools()}
    warnings: list[str] = []
    for name in allowlist:
        defn = by_name.get(name)
        if defn is None:
            continue
        if not defn.read_only or defn.requires_approval:
            warnings.append(
                f"'{name}' is a higher-risk ({defn.risk_tier.value}) tool that can take "
                "consequential actions — review it before publishing."
            )
    return warnings


def _dedupe(items: list[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication of the (trimmed) question/note list."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return tuple(out)


__all__ = [
    "AssistantDraftService",
    "DraftConfig",
    "DraftResult",
]
