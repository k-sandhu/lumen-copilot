"""Registered ownership of authenticated direct-resource denial guards (#579).

The manifest is intentionally API-facing: it names the actual method/template,
the service that owns INV-1/INV-2 non-disclosure, and the stable attempted action.
Tests reconcile it with FastAPI's live route registry so any new authenticated
target-bearing route cannot silently omit the canonical recorder.
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


# These non-path-target operations have confirmed denial ownership: upload
# checks a body-selected collection, chat/schedule creation select an assistant,
# and managed-source creation has a service-local role guard. Other list/create
# routes are intentionally absent.
EXPLICIT_GUARDED_NON_TARGET_ROUTES = frozenset(
    {
        ("POST", "/api/v2/document-uploads"),
        ("POST", "/api/v1/sources"),
        ("POST", "/api/v1/chat/sessions"),
        ("POST", "/api/v1/schedules"),
    }
)


# Retired endpoints authenticate, then unconditionally return 410 without a
# resource lookup. They are classified, but have no terminal 403/404 to audit.
RETIRED_RESOURCE_ROUTES = {
    ("POST", "/api/v1/documents"): "document_upload_retired",
    ("GET", "/api/v1/documents/{document_id}/content"): "document_content_retired",
}

# Non-route target seams added by the media lifecycle also use the same helper.
DIRECT_RESOURCE_SERVICE_GUARDS = (
    ("IngestionAdminService", "report", "ingestion.shadow.report"),
    ("IngestionAdminService", "preview", "ingestion.reingestion.preview"),
    ("IngestionAdminService", "replay", "ingestion.original.replay"),
    ("IngestionAdminService", "execute_generation", "ingestion.reingestion.execute"),
    ("DocumentUploadService", "expire_if_needed", "document_upload.expire"),
    ("DocumentUploadService", "recover_completing", "document_upload.recover"),
)

# These new system/control seams have no authenticated resource 403/404. Their
# typed outcomes retain main's ownership rather than inventing user attribution.
NON_RESOURCE_DENIAL_SEAMS = {
    "DocumentUploadService.audit_rejection": (
        "non-permission lifecycle error sink; no resource lookup or terminal denial"
    ),
    "app.ingestion.reembed._main": "system inventory/reservation CLI; no user authorization guard",
    "app.tasks.upload_janitor.sweep_expired_uploads_async": (
        "system discovery skips vanished candidates; recovery uses DocumentUploadService"
    ),
    "app.services.tools.gate.PolicyApprovalGate": (
        "INV-7 admission returns a recorded approval/refusal, not a resource 403/404"
    ),
    "app.services.tools.runner.ToolRunner": (
        "tool refusals use tool.invoked/tool.result; T2 intent/results retain durable AuditSink"
    ),
    "app.services.transcript_recall.SessionTranscriptReader.recall": (
        "internal runtime-bound current-session read; the model cannot choose session/message ids; "
        "missing/wrong-owner bindings return no turns and log miswiring, not a resource 403/404; "
        "the tool invocation/result retain ToolRunner audit ownership"
    ),
}


DIRECT_RESOURCE_DENIAL_GUARDS = (
    DenialGuardRoute(
        "WEBSOCKET", "/ws/chat/{stream_id}", "ChatStreamAccessService", "chat.stream.subscribe"
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/admin/assistants/{assistant_id}/certify",
        "AssistantGovernanceService",
        "assistant.certify",
        "require_roles",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/admin/assistants/{assistant_id}/feature",
        "AssistantGovernanceService",
        "assistant.feature",
        "require_roles",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/admin/assistants/{assistant_id}/disable",
        "AssistantGovernanceService",
        "assistant.disable",
        "require_roles",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/admin/assistants/{assistant_id}/transfer-ownership",
        "AssistantGovernanceService",
        "assistant.transfer_ownership",
        "require_roles",
    ),
    DenialGuardRoute(
        "GET", "/api/v1/assistants/{assistant_id}", "AssistantsService", "assistant.read"
    ),
    DenialGuardRoute(
        "PATCH", "/api/v1/assistants/{assistant_id}", "AssistantsService", "assistant.update"
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/assistants/{assistant_id}", "AssistantsService", "assistant.delete"
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/assistants/{assistant_id}/publish",
        "AssistantsService",
        "assistant.publish",
    ),
    DenialGuardRoute(
        "GET",
        "/api/v1/assistants/{assistant_id}/versions",
        "AssistantsService",
        "assistant.versions.read",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/assistants/{assistant_id}/rollback",
        "AssistantsService",
        "assistant.rollback",
    ),
    DenialGuardRoute(
        "POST", "/api/v1/assistants/{assistant_id}/test", "AssistantTestService", "assistant.test"
    ),
    DenialGuardRoute("POST", "/api/v1/chat/sessions", "ChatService", "chat.session.create"),
    DenialGuardRoute(
        "GET", "/api/v1/chat/sessions/{session_id}", "ChatService", "chat.session.read"
    ),
    DenialGuardRoute(
        "GET", "/api/v1/chat/sessions/{session_id}/usage", "ChatService", "chat.session.usage"
    ),
    DenialGuardRoute(
        "PATCH", "/api/v1/chat/sessions/{session_id}", "ChatService", "chat.session.update"
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/chat/sessions/{session_id}", "ChatService", "chat.session.delete"
    ),
    DenialGuardRoute(
        "GET", "/api/v1/chat/sessions/{session_id}/messages", "ChatService", "chat.messages.read"
    ),
    DenialGuardRoute(
        "POST", "/api/v1/chat/sessions/{session_id}/messages", "ChatService", "chat.message.send"
    ),
    DenialGuardRoute(
        "GET",
        "/api/v1/chat/sessions/{session_id}/sandbox",
        "SandboxSessionService",
        "sandbox_session.read",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v1/chat/sessions/{session_id}/sandbox/reset",
        "SandboxSessionService",
        "sandbox_session.reset",
    ),
    DenialGuardRoute(
        "DELETE",
        "/api/v1/chat/sessions/{session_id}/sandbox",
        "SandboxSessionService",
        "sandbox_session.close",
    ),
    DenialGuardRoute(
        "GET", "/api/v1/code-runs/{code_run_id}", "SandboxReadService", "code_run.read"
    ),
    DenialGuardRoute(
        "POST", "/api/v1/code-runs/{code_run_id}/cancel", "SandboxSessionService", "code_run.cancel"
    ),
    DenialGuardRoute(
        "GET", "/api/v1/collections/{collection_id}", "CollectionsService", "collection.read"
    ),
    DenialGuardRoute(
        "PATCH", "/api/v1/collections/{collection_id}", "CollectionsService", "collection.update"
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/collections/{collection_id}", "CollectionsService", "collection.delete"
    ),
    DenialGuardRoute("GET", "/api/v1/runs/{run_id}", "RunsReadService", "run.read"),
    DenialGuardRoute("POST", "/api/v1/runs/{run_id}/resume", "RunsControlService", "run.resume"),
    DenialGuardRoute("POST", "/api/v1/runs/{run_id}/cancel", "RunsControlService", "run.cancel"),
    DenialGuardRoute("POST", "/api/v1/runs/{run_id}/reroute", "RunsControlService", "run.reroute"),
    DenialGuardRoute("POST", "/api/v1/schedules", "SchedulesService", "schedule.create"),
    DenialGuardRoute("GET", "/api/v1/schedules/{schedule_id}", "SchedulesService", "schedule.read"),
    DenialGuardRoute(
        "PATCH", "/api/v1/schedules/{schedule_id}", "SchedulesService", "schedule.update"
    ),
    DenialGuardRoute(
        "DELETE", "/api/v1/schedules/{schedule_id}", "SchedulesService", "schedule.delete"
    ),
    DenialGuardRoute(
        "POST", "/api/v1/schedules/{schedule_id}/pause", "SchedulesService", "schedule.pause"
    ),
    DenialGuardRoute(
        "POST", "/api/v1/schedules/{schedule_id}/resume", "SchedulesService", "schedule.resume"
    ),
    DenialGuardRoute(
        "POST", "/api/v1/schedules/{schedule_id}/run-now", "SchedulesService", "schedule.run_now"
    ),
    DenialGuardRoute(
        "POST", "/api/v2/document-uploads", "DocumentUploadService", "document.upload"
    ),
    DenialGuardRoute(
        "GET",
        "/api/v2/document-uploads/{upload_id}",
        "DocumentUploadService",
        "document_upload.read",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v2/document-uploads/{upload_id}/parts",
        "DocumentUploadService",
        "document_upload.sign_parts",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v2/document-uploads/{upload_id}/complete",
        "DocumentUploadService",
        "document_upload.complete",
    ),
    DenialGuardRoute(
        "DELETE",
        "/api/v2/document-uploads/{upload_id}",
        "DocumentUploadService",
        "document_upload.abort",
    ),
    DenialGuardRoute(
        "POST",
        "/api/v2/documents/{document_id}/access-url",
        "DocumentAccessService",
        "document.access_url",
    ),
    DenialGuardRoute(
        "GET",
        "/api/v2/documents/{document_id}/transcript",
        "DocumentAccessService",
        "document.transcript.read",
    ),
    DenialGuardRoute("GET", "/api/v1/documents/{document_id}", "DocumentService", "document.read"),
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
    "DIRECT_RESOURCE_SERVICE_GUARDS",
    "EXPLICIT_GUARDED_NON_TARGET_ROUTES",
    "NON_RESOURCE_DENIAL_SEAMS",
    "RETIRED_RESOURCE_ROUTES",
    "DenialGuardRoute",
]
