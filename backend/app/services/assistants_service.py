"""Assistant CRUD + publish/version/rollback use-cases (ADR-0011, issue #211).

The orchestration layer for the ``/assistants`` surface (ADR-0004: ``services/``
compose adapters; routers call exactly one service). It pairs the tenant-scoped
``db/`` :class:`~app.db.repositories.AssistantRepository` +
:class:`~app.db.repositories.AssistantVersionRepository` (the only SQL) with the
CC-A tool registry (allow-list validation) and the knowledge-scope ownership
check, and audits every mutation through the one sink (INV-6).

**An assistant is a saved, governed config, not a new engine.** It holds the three
inputs the chat runtime already consumes — ``instructions`` → system prompt,
``tool_allowlist`` → the CC-A allowed-tool set, ``knowledge_scope`` → the
retrieval filter — plus a model default, an accountable owner + backup, an
autonomy level, and a lifecycle status. Publishing freezes the mutable head into
an immutable :class:`~app.domain.entities.AssistantVersion`; a session pins that
version so a transcript is reproducible.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repository) *and* the
caller's ownership (this service). An assistant in another tenant — or owned by
another user (and not granted) — is treated as **non-existent**: the read/mutate
raises :class:`~app.core.errors.NotFoundError` (404, existence non-disclosure;
never 403). The ``tenant_id``/``owner_id`` come from the resolved principal, never
request input.

**Validation (INV-8, fail-closed).** A ``tool_allowlist`` entry not in the CC-A
registry → **422**. A ``knowledge_scope`` collection/source id the caller neither
owns nor is granted → **422**. Publishing without a distinct backup owner → **422**
(the frozen F-AB-0 contract's chosen code for the illegal transition; ADR-0011 §4
motivates the rule). An unknown/malformed rollback ``version`` → **422**.

**No-widen (INV-2).** This service only *validates* that a scope's ids are
visible to the owner at configure time; the actual narrowing is enforced at run
time inside ``retrieval/`` keyed off the *running* principal (the assembler in
``services.assistant_runtime`` passes the scope as an additional filter — it can
only intersect, never widen, what that principal may already retrieve).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    CollectionRepository,
    GrantRepository,
    McpServerRepository,
    SourceRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    Assistant,
    AssistantStatus,
    AssistantVersion,
    AuditOutcome,
    AutonomyLevel,
    GrantPrincipalType,
    GrantResourceType,
    KnowledgeMode,
    KnowledgeScope,
    Role,
)
from app.services.audit import AuditSink
from app.services.autonomy_policy_service import AutonomyPolicyReader
from app.services.tools.mcp_bridge import (
    is_mcp_tool_name,
    namespaced_tool_name,
    slug_for_server,
)
from app.services.tools.registry import registered_names

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# The sentinel the API layer uses to distinguish "field omitted" from "field set
# to null" on a PATCH (a nullable field can legitimately be cleared to None). A
# caller passes UNSET for an untouched field; None explicitly clears it.
UNSET: object = object()

_ASSISTANT_CURSOR_PREFIX = "ast:"
_VERSION_CURSOR_PREFIX = "asv:"


def _encode_cursor(prefix: str, row_id: UUID) -> str:
    raw = f"{prefix}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(prefix: str, cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(prefix):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(prefix) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


@dataclass(frozen=True, slots=True)
class AssistantPage:
    """One page of assistants + the opaque cursor for the next page."""

    items: list[Assistant]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class VersionPage:
    """One page of immutable versions (newest first) + the next cursor."""

    items: list[AssistantVersion]
    next_cursor: str | None


def config_from_assistant(assistant: Assistant) -> dict[str, object]:
    """The full frozen config captured in a version (ADR-0011 §1, contract shape).

    Serialises the mutable head's run-affecting fields — everything the runtime
    consumes — to the ``AssistantVersionConfig`` jsonb shape (camelCase per the
    frozen contract) so a pinned version reproduces the exact behaviour. Owner /
    status / timestamps are *not* frozen (they are head-level governance, not run
    config).
    """
    scope = assistant.knowledge_scope
    return {
        "name": assistant.name,
        "description": assistant.description,
        "instructions": assistant.instructions,
        "model": assistant.model,
        "knowledgeScope": {
            "collectionIds": [str(c) for c in scope.collection_ids],
            "sourceIds": [str(s) for s in scope.source_ids],
            "modes": [m.value for m in scope.modes],
        },
        "toolAllowlist": list(assistant.tool_allowlist),
        "autonomyLevel": assistant.autonomy_level.value,
    }


class AssistantsService:
    """Create / read / update / delete / publish / version / rollback assistants.

    Constructed per-request with the session, the resolved ``tenant_id`` /
    ``owner_id`` + the caller's ``roles`` (all from the token, never request
    input), and the audit sink + correlation context. All ownership/tenancy
    enforcement lives here; the repository enforces tenancy (INV-1) and this
    service enforces the owner-or-admin rule and the validation invariants.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        roles: tuple[Role, ...],
        audit: AuditSink,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._session = session
        self._assistants = AssistantRepository(session, tenant_id)
        self._versions = AssistantVersionRepository(session, tenant_id)
        self._collections = CollectionRepository(session, tenant_id)
        self._sources = SourceRepository(session, tenant_id)
        self._grants = GrantRepository(session, tenant_id)
        self._mcp_servers = McpServerRepository(session, tenant_id)
        # The per-tenant autonomy cap (issue #218) the publish path enforces — an
        # assistant may not be published above the tenant ceiling. Tenant-scoped (INV-1).
        self._autonomy_cap = AutonomyPolicyReader(session, tenant_id=tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._is_admin = Role.ADMIN in roles
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    # --- authorization ------------------------------------------------------

    def _may_manage(self, assistant: Assistant) -> bool:
        """Whether the caller may edit/publish/delete ``assistant`` (owner or admin)."""
        return self._is_admin or assistant.owner_id == self._owner_id

    async def _load_managed_or_404(self, assistant_id: UUID) -> Assistant:
        """Load an assistant the caller may manage, or raise 404 (existence non-disclosure).

        Deny-by-default: the assistant must exist in this tenant **and** be owned
        by the caller (or the caller must be a tenant admin). A missing,
        cross-tenant, or non-owned id is a 404 — a non-owner is indistinguishable
        from a non-existent id (INV-1/INV-2), so the surface never reveals that a
        foreign/unowned assistant exists.
        """
        assistant = await self._assistants.get(assistant_id)
        if assistant is None or not self._may_manage(assistant):
            raise NotFoundError("Assistant not found.")
        return assistant

    async def _with_current_version(self, assistant: Assistant) -> Assistant:
        """Fill the ``current_version`` + ``effective_autonomy`` projections (visibility).

        ``current_version`` is the published head number (``None`` while never
        published). ``effective_autonomy`` is the assistant's autonomy after the tenant
        admin cap (``min(configured, cap)``, issue #218) — surfaced so the library/run
        detail can show how far the assistant may *actually* act. The cap is resolved
        once per read; absence of a cap ⇒ the configured level (no ceiling).
        """
        head = await self._versions.get_head(assistant.id)
        effective_autonomy = await self._autonomy_cap.clamp(assistant.autonomy_level)
        return Assistant(
            id=assistant.id,
            tenant_id=assistant.tenant_id,
            owner_id=assistant.owner_id,
            backup_owner_id=assistant.backup_owner_id,
            name=assistant.name,
            description=assistant.description,
            instructions=assistant.instructions,
            model=assistant.model,
            knowledge_scope=assistant.knowledge_scope,
            tool_allowlist=assistant.tool_allowlist,
            autonomy_level=assistant.autonomy_level,
            status=assistant.status,
            certification_state=assistant.certification_state,
            featured=assistant.featured,
            category=assistant.category,
            disabled_at=assistant.disabled_at,
            created_at=assistant.created_at,
            updated_at=assistant.updated_at,
            current_version=head.version if head is not None else None,
            effective_autonomy=effective_autonomy,
            owner_orphaned=assistant.owner_orphaned,
        )

    # --- validation ---------------------------------------------------------

    async def _validate_tool_allowlist(self, tool_allowlist: tuple[str, ...]) -> None:
        """Every allow-list entry must be a real tool the caller may grant (422 else).

        Deny-by-default (issue #207 / #227): an unknown tool name in the allow-list
        is a configuration error caught here, so an assistant can never be saved
        referencing a tool that does not exist — and the runner's allow-list is
        always a subset of the real registry. ``[]`` is valid (the ad-hoc default set
        at run time). Two namespaces are validated against two sources of truth:

        * a **native** name against the static CC-A registry (``registered_names``);
        * an **``mcp:*``** name against *this caller's own* registered MCP tools
          (ADR-0012 §6). MCP tools are tenant-/owner-scoped and dynamic, so they are
          NOT in the global registry — validating them against the caller's own
          servers keeps INV-1/INV-2 (an assistant can never name an MCP tool on a
          server the caller did not register, and cross-tenant server ids are never
          even considered).
        """
        native = {t for t in tool_allowlist if not is_mcp_tool_name(t)}
        mcp = {t for t in tool_allowlist if is_mcp_tool_name(t)}
        unknown = [t for t in native if t not in registered_names()]
        if mcp:
            known_mcp = await self._known_mcp_tool_names()
            unknown.extend(t for t in mcp if t not in known_mcp)
        if unknown:
            raise ValidationError(
                f"Unknown tool(s) in allow-list: {', '.join(sorted(set(unknown)))}.",
                code="unknown_tool",
            )

    async def _known_mcp_tool_names(self) -> set[str]:
        """The namespaced MCP tool names the caller may grant (own registered servers).

        Every discovered tool of every server this owner registered in this tenant,
        namespaced ``mcp:<slug>:<tool>`` — including servers not currently ``enabled``
        (a temporarily-disabled server's tools stay *nameable* in the builder; the
        run-time resolver, not this configure-time check, is what refuses a disabled
        server, so re-enabling it does not require re-editing the assistant). A
        foreign-tenant/non-owned server is never listed, so its tools can never be
        named (INV-1/INV-2).
        """
        servers = await self._mcp_servers.list_for_owner_page(self._owner_id, limit=_MAX_LIMIT)
        names: set[str] = set()
        for server in servers:
            slug = slug_for_server(server)
            for raw in server.discovered_tools:
                if not isinstance(raw, dict):
                    continue
                raw_name = str(raw.get("name", "")).strip()
                if raw_name:
                    names.add(namespaced_tool_name(slug, raw_name))
        return names

    async def _validate_knowledge_scope(self, scope: KnowledgeScope) -> None:
        """Every scoped collection/source id must be owned-or-granted (422 else).

        A ``knowledge_scope`` can only *narrow* what the running principal may
        retrieve (INV-2); scoping to a collection/source the owner cannot see would
        be a meaningless (and confusing) filter, so it is rejected at configure
        time. Ownership is checked against the owner's own resources; grants extend
        visibility (the existing ``grants`` pattern, ADR-0011 §1). The runtime still
        re-filters per the *running* user — this is a configure-time sanity gate,
        not the enforcement point.
        """
        if not scope.collection_ids and not scope.source_ids:
            return
        granted_collections, granted_documents = await self._granted_resource_ids()
        for collection_id in scope.collection_ids:
            if collection_id in granted_collections:
                continue
            collection = await self._collections.get(collection_id)
            if collection is None or collection.owner_id != self._owner_id:
                raise ValidationError(
                    f"Knowledge scope references a collection you cannot access: {collection_id}.",
                    code="scope_collection_forbidden",
                )
        for source_id in scope.source_ids:
            source = await self._sources.get(source_id)
            if source is None or source.owner_id != self._owner_id:
                raise ValidationError(
                    f"Knowledge scope references a source you cannot access: {source_id}.",
                    code="scope_source_forbidden",
                )

    async def _granted_resource_ids(self) -> tuple[set[UUID], set[UUID]]:
        """The (collection ids, document ids) explicitly granted to the caller."""
        grants = await self._grants.list_for_principal(
            principal_type=GrantPrincipalType.USER, principal_id=self._owner_id
        )
        collections = {
            g.resource_id for g in grants if g.resource_type == GrantResourceType.COLLECTION
        }
        documents = {
            g.resource_id for g in grants if g.resource_type == GrantResourceType.DOCUMENT
        }
        return collections, documents

    async def _validate_backup_owner(self, backup_owner_id: UUID | None) -> None:
        """A backup owner (if set) must be a distinct user in the same tenant (422 else)."""
        if backup_owner_id is None:
            return
        if backup_owner_id == self._owner_id:
            raise ValidationError(
                "The backup owner must be different from the owner.",
                code="backup_owner_same_as_owner",
            )

    async def _enforce_autonomy_cap(self, autonomy_level: AutonomyLevel) -> None:
        """Reject publishing an assistant above the tenant autonomy cap (422; issue #218).

        The EFFECTIVE autonomy is ``min(assistant, tenant cap)``; publishing an
        assistant whose configured level EXCEEDS the cap is rejected as **422** (INV-8
        — an illegal transition) so a caller cannot ship an over-autonomous assistant.
        Absence of a cap ⇒ no ceiling (the permissive default), so this is a no-op then.
        Tenant-scoped (INV-1).
        """
        cap = await self._autonomy_cap.effective_cap()
        if autonomy_level.rank > cap.rank:
            raise ValidationError(
                f"The assistant's autonomy ({autonomy_level.value!r}) exceeds the "
                f"tenant cap ({cap.value!r}). Lower the assistant's autonomy or ask an "
                "admin to raise the cap before publishing.",
                code="autonomy_above_cap",
            )

    # --- audit --------------------------------------------------------------

    async def _emit_audit(
        self,
        *,
        action: AuditAction,
        assistant_id: UUID,
        metadata: dict[str, object],
    ) -> None:
        await self._audit.emit(
            action=action,
            actor=AuditActor.user(self._owner_id),
            resource_type="assistant",
            resource_id=str(assistant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata=metadata,
        )

    # --- CRUD use-cases -----------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        description: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
        knowledge_scope: KnowledgeScope | None = None,
        tool_allowlist: tuple[str, ...] = (),
        autonomy_level: AutonomyLevel = AutonomyLevel.SUGGEST,
        backup_owner_id: UUID | None = None,
    ) -> Assistant:
        """Create a ``draft`` assistant owned by the caller (ADR-0011 §1).

        Validates the allow-list against the CC-A registry, the knowledge scope's
        ids against ownership/grants, and the backup owner (if any) — all 422 on
        failure, nothing persisted. Only ``name`` is required; the server owns
        ``owner``/``status``/timestamps.
        """
        if not name.strip():
            raise ValidationError("Assistant name must not be blank.", code="invalid_name")
        scope = knowledge_scope or KnowledgeScope.empty()
        await self._validate_tool_allowlist(tool_allowlist)
        await self._validate_knowledge_scope(scope)
        await self._validate_backup_owner(backup_owner_id)

        assistant = await self._assistants.create(
            owner_id=self._owner_id,
            name=name,
            description=description,
            instructions=instructions,
            model=model,
            knowledge_scope=scope,
            tool_allowlist=tool_allowlist,
            autonomy_level=autonomy_level,
            backup_owner_id=backup_owner_id,
        )
        await self._emit_audit(
            action=AuditAction.ASSISTANT_CREATED,
            assistant_id=assistant.id,
            metadata={"name": assistant.name, "status": assistant.status.value},
        )
        # Fill the projections (current_version / effective_autonomy) so a freshly
        # created assistant carries the same shape as a read (issue #218 visibility).
        return await self._with_current_version(assistant)

    async def get(self, assistant_id: UUID) -> Assistant:
        """Fetch one assistant the caller may see, or 404 (INV-1/INV-2)."""
        assistant = await self._load_managed_or_404(assistant_id)
        return await self._with_current_version(assistant)

    async def list_(
        self,
        *,
        cursor: str | None,
        limit: int | None,
        status: AssistantStatus | None = None,
    ) -> AssistantPage:
        """A keyset page of the caller's own assistants (newest first)."""
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(_ASSISTANT_CURSOR_PREFIX, cursor) if cursor else None
        rows = await self._assistants.list_for_owner_page(
            self._owner_id, limit=page_size + 1, after_id=after_id, status=status
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = (
            _encode_cursor(_ASSISTANT_CURSOR_PREFIX, page[-1].id) if has_more and page else None
        )
        items = [await self._with_current_version(a) for a in page]
        return AssistantPage(items=items, next_cursor=next_cursor)

    async def update(
        self,
        assistant_id: UUID,
        *,
        name: object = UNSET,
        description: object = UNSET,
        instructions: object = UNSET,
        model: object = UNSET,
        knowledge_scope: object = UNSET,
        tool_allowlist: object = UNSET,
        autonomy_level: object = UNSET,
        backup_owner_id: object = UNSET,
    ) -> Assistant:
        """Patch the mutable head; not visible/owned → 404, invalid → 422.

        Only the fields the caller passed (not ``UNSET``) are touched. A nullable
        field may be explicitly cleared to ``None``. The allow-list and knowledge
        scope are re-validated when supplied; the backup owner (if changed) must be
        a distinct user.
        """
        await self._load_managed_or_404(assistant_id)

        fields: dict[str, object] = {}
        if name is not UNSET:
            if not str(name).strip():
                raise ValidationError("Assistant name must not be blank.", code="invalid_name")
            fields["name"] = name
        if description is not UNSET:
            fields["description"] = description
        if instructions is not UNSET:
            fields["instructions"] = instructions
        if model is not UNSET:
            fields["model"] = model
        if knowledge_scope is not UNSET:
            assert isinstance(knowledge_scope, KnowledgeScope)
            await self._validate_knowledge_scope(knowledge_scope)
            fields["knowledge_scope"] = knowledge_scope
        if tool_allowlist is not UNSET:
            assert isinstance(tool_allowlist, list)
            allowlist = tuple(str(t) for t in tool_allowlist)
            await self._validate_tool_allowlist(allowlist)
            fields["tool_allowlist"] = allowlist
        if autonomy_level is not UNSET:
            assert isinstance(autonomy_level, AutonomyLevel)
            fields["autonomy_level"] = autonomy_level
        if backup_owner_id is not UNSET:
            backup: UUID | None = None
            if backup_owner_id is not None:
                assert isinstance(backup_owner_id, UUID)
                backup = backup_owner_id
            await self._validate_backup_owner(backup)
            fields["backup_owner_id"] = backup

        updated = await self._assistants.update(assistant_id, fields=fields)
        if updated is None:  # pragma: no cover — visibility already established
            raise NotFoundError("Assistant not found.")
        await self._emit_audit(
            action=AuditAction.ASSISTANT_UPDATED,
            assistant_id=assistant_id,
            metadata={"fields": sorted(fields.keys())},
        )
        return await self._with_current_version(updated)

    async def delete(self, assistant_id: UUID) -> None:
        """Delete an assistant the caller owns (cascades to versions), or 404."""
        assistant = await self._load_managed_or_404(assistant_id)
        await self._assistants.delete(assistant.id)
        await self._emit_audit(
            action=AuditAction.ASSISTANT_DELETED,
            assistant_id=assistant.id,
            metadata={"name": assistant.name},
        )

    # --- publish / version / rollback --------------------------------------

    async def publish(self, assistant_id: UUID, *, notes: str | None = None) -> AssistantVersion:
        """Freeze the head into a new immutable version and mark it published (ADR-0011 §1).

        Requires both an owner and a **distinct** backup owner (ADR-0011 §4); a
        publish without a backup owner is rejected as **422** (the frozen F-AB-0
        contract's chosen code for this illegal transition, INV-8). Freezes the
        current head config into ``version = max+1`` and sets ``status =
        published`` (idempotent re-publish appends a further version). Audited
        ``assistant.published`` (INV-6).
        """
        assistant = await self._load_managed_or_404(assistant_id)
        if assistant.backup_owner_id is None:
            raise ValidationError(
                "An assistant requires a backup owner before it can be published.",
                code="backup_owner_required",
            )
        if assistant.backup_owner_id == assistant.owner_id:
            raise ValidationError(
                "The backup owner must be different from the owner.",
                code="backup_owner_same_as_owner",
            )
        # Enforce the tenant autonomy cap (issue #218, AC-2): an assistant may not be
        # published ABOVE the tenant ceiling. This is a hard reject (422 / INV-8) so the
        # owner cannot ship an assistant whose configured autonomy exceeds what the
        # admin permits — they must lower the assistant's autonomy (or an admin must
        # raise the cap) first. Tenant-scoped (INV-1). Absence of a cap ⇒ no ceiling.
        await self._enforce_autonomy_cap(assistant.autonomy_level)
        # Re-validate the frozen config's allow-list + scope so a published version
        # is never internally inconsistent (a tool removed from the registry since
        # the draft was saved would block publish, not ship a dangling reference).
        await self._validate_tool_allowlist(assistant.tool_allowlist)
        await self._validate_knowledge_scope(assistant.knowledge_scope)

        version_number = await self._versions.next_version(assistant.id)
        version = await self._versions.add(
            assistant_id=assistant.id,
            version=version_number,
            author_id=self._owner_id,
            config=config_from_assistant(assistant),
            notes=notes,
        )
        if assistant.status is not AssistantStatus.PUBLISHED:
            await self._assistants.update(
                assistant.id, fields={"status": AssistantStatus.PUBLISHED}
            )
        await self._emit_audit(
            action=AuditAction.ASSISTANT_PUBLISHED,
            assistant_id=assistant.id,
            metadata={"version": version.version},
        )
        return version

    async def list_versions(
        self, assistant_id: UUID, *, cursor: str | None, limit: int | None
    ) -> VersionPage:
        """A keyset page of an assistant's immutable versions (newest first); not visible → 404."""
        await self._load_managed_or_404(assistant_id)
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(_VERSION_CURSOR_PREFIX, cursor) if cursor else None
        rows = await self._versions.list_for_assistant_page(
            assistant_id, limit=page_size + 1, after_id=after_id
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = (
            _encode_cursor(_VERSION_CURSOR_PREFIX, page[-1].id) if has_more and page else None
        )
        return VersionPage(items=page, next_cursor=next_cursor)

    async def rollback(
        self, assistant_id: UUID, *, version: int, notes: str | None = None
    ) -> AssistantVersion:
        """Roll the head back to a prior version by appending a NEW version (ADR-0011 §1).

        History is never mutated or deleted: rollback copies the named prior
        version's ``config`` into a brand-new head version (``version = max+1``) and
        re-applies that config to the mutable head, then re-publishes. An unknown /
        malformed ``version`` → **422** (INV-8). Audited ``assistant.rolled_back``.
        """
        assistant = await self._load_managed_or_404(assistant_id)
        if version < 1:
            raise ValidationError("Version must be a positive integer.", code="invalid_version")
        target = await self._versions.get_by_number(assistant.id, version)
        if target is None:
            raise ValidationError(
                f"No such version {version} for this assistant.", code="unknown_version"
            )
        new_number = await self._versions.next_version(assistant.id)
        restored = await self._versions.add(
            assistant_id=assistant.id,
            version=new_number,
            author_id=self._owner_id,
            config=dict(target.config),
            notes=notes,
            diff_summary=f"rollback to v{version}",
        )
        # Re-apply the restored config to the mutable head so a new session started
        # from the head runs the rolled-back behaviour, and ensure it is published.
        await self._apply_config_to_head(assistant.id, target.config)
        await self._emit_audit(
            action=AuditAction.ASSISTANT_ROLLED_BACK,
            assistant_id=assistant.id,
            metadata={"from_version": version, "new_version": new_number},
        )
        return restored

    async def _apply_config_to_head(self, assistant_id: UUID, config: dict[str, object]) -> None:
        """Restore a frozen ``config`` onto the mutable head (rollback), tenant-scoped."""
        scope_raw = config.get("knowledgeScope")
        scope = _scope_from_config(scope_raw)
        fields: dict[str, object] = {
            "name": config.get("name"),
            "description": config.get("description"),
            "instructions": config.get("instructions"),
            "model": config.get("model"),
            "knowledge_scope": scope,
            "tool_allowlist": [str(t) for t in _as_list(config.get("toolAllowlist"))],
            "autonomy_level": _autonomy_from_config(config.get("autonomyLevel")),
            "status": AssistantStatus.PUBLISHED,
        }
        await self._assistants.update(assistant_id, fields=fields)


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _scope_from_config(raw: object) -> KnowledgeScope:
    """Rebuild a :class:`KnowledgeScope` from a frozen config's camelCase blob."""
    data = raw if isinstance(raw, dict) else {}

    def _uuids(key: str) -> tuple[UUID, ...]:
        out: list[UUID] = []
        for v in _as_list(data.get(key)):
            try:
                out.append(UUID(str(v)))
            except (ValueError, TypeError):
                continue
        return tuple(out)

    modes: list[KnowledgeMode] = []
    for m in _as_list(data.get("modes")):
        try:
            modes.append(KnowledgeMode(str(m)))
        except ValueError:
            continue
    return KnowledgeScope(
        collection_ids=_uuids("collectionIds"),
        source_ids=_uuids("sourceIds"),
        modes=tuple(modes),
    )


def _autonomy_from_config(raw: object) -> AutonomyLevel:
    try:
        return AutonomyLevel(str(raw))
    except ValueError:
        return AutonomyLevel.SUGGEST


__all__ = [
    "UNSET",
    "AssistantPage",
    "AssistantsService",
    "VersionPage",
    "config_from_assistant",
]
