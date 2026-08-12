"""Registered ownership of authenticated direct-resource denial guards (#579).

The manifest is intentionally API-facing: it names the actual method/template,
the service that owns INV-1/INV-2 non-disclosure, and the stable attempted action.
Tests reconcile it with FastAPI's live route registry so a new route in a
confirmed resource family cannot silently omit the canonical recorder.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DenialGuardRoute:
    method: str
    path: str
    owner: str
    attempted_action: str
    role_owner: str | None = None


# A dynamic target route registered below these prefixes must declare its guard
# owner.  List/create routes without a path target are deliberately ignored by
# the mechanical scan unless named in the explicit set below.
GOVERNED_DENIAL_PREFIXES = (
    "/api/v1/documents",
    "/api/v1/mcp-servers",
    "/api/v1/sources",
    "/api/v1/artifacts",
    "/api/v1/saved-searches",
    "/api/v1/run-deliveries",
    "/api/v1/admin/groups",
    "/api/v1/admin/llm-providers",
    "/api/v1/admin/members",
)

# These two non-path-target operations have confirmed denial ownership: upload
# checks a body-selected collection, while managed-source creation has a
# service-local role guard.  Other list/create routes are intentionally absent.
EXPLICIT_GUARDED_NON_TARGET_ROUTES = frozenset(
    {
        ("POST", "/api/v1/documents"),
        ("POST", "/api/v1/sources"),
    }
)


DIRECT_RESOURCE_DENIAL_GUARDS = (
    DenialGuardRoute("POST", "/api/v1/documents", "DocumentService", "document.upload"),
    DenialGuardRoute("GET", "/api/v1/documents/{document_id}", "DocumentService", "document.read"),
    DenialGuardRoute(
        "GET", "/api/v1/documents/{document_id}/content", "DocumentService", "document.download"
    ),
    DenialGuardRoute(
        "GET", "/api/v1/documents/{document_id}/text", "DocumentService", "document.text.read"
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/documents/{document_id}", "DocumentService", "document.delete"
    ),
    DenialGuardRoute(
        "GET", "/api/v1/mcp-servers/{server_id}", "McpServersService", "mcp_server.read"
    ),
    DenialGuardRoute(
        "PATCH", "/api/v1/mcp-servers/{server_id}", "McpServersService", "mcp_server.update"
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/mcp-servers/{server_id}", "McpServersService", "mcp_server.delete"
    ),
    DenialGuardRoute(
        "POST", "/api/v1/mcp-servers/{server_id}/test", "McpServersService", "mcp_server.test"
    ),
    DenialGuardRoute(
        "GET", "/api/v1/mcp-servers/{server_id}/tools", "McpServersService", "mcp_server.tools.read"
    ),
    DenialGuardRoute(
        "POST", "/api/v1/sources", "SourcesService", "source.create", "SourcesService"
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/sources/{source_id}/sync",
        "SourcesService",
        "source.sync",
        "SourcesService",
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/sources/{source_id}", "SourcesService", "source.delete", "SourcesService"
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/sources/{source_id}/connect",
        "ConnectorOAuthService",
        "source.connect",
        "ConnectorOAuthService",
    ),
    DenialGuardRoute("GET", "/api/v1/artifacts/{artifact_id}", "ArtifactsService", "artifact.read"),
    DenialGuardRoute(
        "GET", "/api/v1/artifacts/{artifact_id}/content", "ArtifactsService", "artifact.download"
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/artifacts/{artifact_id}", "ArtifactsService", "artifact.delete"
    ),
    DenialGuardRoute(
        "GET", "/api/v1/saved-searches/{saved_search_id}", "SavedSearchService", "saved_search.read"
    ),
    DenialGuardRoute(
        "PATCH",
        "/api/v1/saved-searches/{saved_search_id}",
        "SavedSearchService",
        "saved_search.update",
    ),
    DenialGuardRoute(
        "DELETE",
        "/api/v1/saved-searches/{saved_search_id}",
        "SavedSearchService",
        "saved_search.delete",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/run-deliveries/{delivery_id}/read",
        "RunDeliveryService",
        "run.delivery.read",
    ),
    DenialGuardRoute(
        "GET", "/api/v1/admin/groups/{group_id}", "GroupsService", "group.read", "require_roles"
    ),
    DenialGuardRoute(
        "PATCH", "/api/v1/admin/groups/{group_id}", "GroupsService", "group.update", "require_roles"
    ),
    DenialGuardRoute(
        "DELETE",
        "/api/v1/admin/groups/{group_id}",
        "GroupsService",
        "group.delete",
        "require_roles",
    ),
    DenialGuardRoute(
        "GET",
        "/api/v1/admin/groups/{group_id}/members",
        "GroupsService",
        "group.members.read",
        "require_roles",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/admin/groups/{group_id}/members",
        "GroupsService",
        "group.member.add",
        "require_roles",
    ),
    DenialGuardRoute(
        "DELETE",
        "/api/v1/admin/groups/{group_id}/members/{member_id}",
        "GroupsService",
        "group.member.remove",
        "require_roles",
    ),
    DenialGuardRoute(
        "PATCH",
        "/api/v1/admin/llm-providers/{provider_id}",
        "LlmProviderService",
        "llm_provider.update",
        "require_roles",
    ),
    DenialGuardRoute(
        "DELETE",
        "/api/v1/admin/llm-providers/{provider_id}",
        "LlmProviderService",
        "llm_provider.delete",
        "require_roles",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/admin/llm-providers/{provider_id}/refresh",
        "LlmProviderService",
        "llm_provider.refresh",
        "require_roles",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/admin/members/{member_id}/attest-identity",
        "AdminService",
        "user.identity.attest",
        "require_roles",
    ),
)


__all__ = [
    "DIRECT_RESOURCE_DENIAL_GUARDS",
    "EXPLICIT_GUARDED_NON_TARGET_ROUTES",
    "GOVERNED_DENIAL_PREFIXES",
    "DenialGuardRoute",
]
