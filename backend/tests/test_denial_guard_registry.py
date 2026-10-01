"""Mechanical completeness checks for authenticated direct-resource denials (#579)."""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from typing import get_args, get_type_hints

import pytest
from fastapi import Depends
from fastapi.routing import APIRoute, APIWebSocketRoute

from app.api.denial_registry import (
    DIRECT_RESOURCE_DENIAL_GUARDS,
    DIRECT_RESOURCE_SERVICE_GUARDS,
    EXPLICIT_GUARDED_NON_TARGET_ROUTES,
    NON_RESOURCE_DENIAL_SEAMS,
    RETIRED_RESOURCE_ROUTES,
)
from app.api.deps import current_user
from app.main import app
from app.sandbox.service import SandboxReadService, SandboxSessionService
from app.services.admin_service import AdminService
from app.services.artifacts_service import ArtifactsService
from app.services.assistant_governance_service import AssistantGovernanceService
from app.services.assistant_test_service import AssistantTestService
from app.services.assistants_service import AssistantsService
from app.services.audit import PermissionDeniedContext, PermissionDeniedRecorder
from app.services.chat_service import ChatService
from app.services.chat_stream_access import ChatStreamAccessService
from app.services.collections_service import CollectionsService
from app.services.connector_oauth_service import ConnectorOAuthService
from app.services.document_service import DocumentService
from app.services.document_upload_service import DocumentAccessService, DocumentUploadService
from app.services.groups_service import GroupsService
from app.services.llm_providers_service import LlmProviderService
from app.services.mcp_servers_service import McpServersService
from app.services.run_delivery_service import RunDeliveryService
from app.services.runs_service import RunsControlService, RunsReadService, enqueue_manual_run
from app.services.saved_searches_service import SavedSearchService
from app.services.schedules_service import SchedulesService
from app.services.sources_service import SourcesService

_OWNER_SEAMS = {
    "AssistantsService": AssistantsService,
    "AssistantGovernanceService": AssistantGovernanceService,
    "AssistantTestService": AssistantTestService,
    "ChatService": ChatService,
    "ChatStreamAccessService": ChatStreamAccessService,
    "CollectionsService": CollectionsService,
    "RunsReadService": RunsReadService,
    "RunsControlService": RunsControlService,
    "SchedulesService": SchedulesService,
    "SandboxReadService": SandboxReadService,
    "SandboxSessionService": SandboxSessionService,
    "enqueue_manual_run": enqueue_manual_run,
    "DocumentService": DocumentService,
    "DocumentUploadService": DocumentUploadService,
    "DocumentAccessService": DocumentAccessService,
    "McpServersService": McpServersService,
    "SourcesService": SourcesService,
    "ConnectorOAuthService": ConnectorOAuthService.start_connect,
    "ArtifactsService": ArtifactsService,
    "SavedSearchService": SavedSearchService,
    "RunDeliveryService": RunDeliveryService,
    "GroupsService": GroupsService,
    "LlmProviderService": LlmProviderService,
    "AdminService": AdminService.attest_member_identity,
}


def _registered_direct_routes(routes):
    def authenticated(dependency):
        return dependency.call is current_user or any(
            authenticated(child) for child in dependency.dependencies
        )

    return {
        (method, route.path)
        for route in routes
        if isinstance(route, APIRoute | APIWebSocketRoute)
        and route.param_convertors
        # WebSocket authentication may be performed inside the endpoint. Every
        # path target therefore requires explicit ownership, independent of how
        # the transport authenticates; non-target health streams remain outside.
        and (isinstance(route, APIWebSocketRoute) or authenticated(route.dependant))
        for method in (
            route.methods - {"HEAD", "OPTIONS"} if isinstance(route, APIRoute) else {"WEBSOCKET"}
        )
    }


def test_direct_resource_guard_manifest_matches_the_registered_api() -> None:
    """Every confirmed family has one named owner/action and no unregistered entry."""
    manifest = {(entry.method, entry.path): entry for entry in DIRECT_RESOURCE_DENIAL_GUARDS}
    assert len(manifest) == len(DIRECT_RESOURCE_DENIAL_GUARDS), "duplicate guard route"
    assert all(entry.owner and entry.attempted_action for entry in manifest.values())
    registered = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
        if method not in {"HEAD", "OPTIONS"}
    } | {("WEBSOCKET", route.path) for route in app.routes if isinstance(route, APIWebSocketRoute)}
    governed_registered = (
        _registered_direct_routes(app.routes) - RETIRED_RESOURCE_ROUTES.keys()
    ) | EXPLICIT_GUARDED_NON_TARGET_ROUTES
    assert (
        set(manifest) == governed_registered
    ), f"missing={governed_registered - set(manifest)}, extra={set(manifest) - governed_registered}"
    assert set(manifest) <= registered
    assert {entry.owner for entry in manifest.values()} | {"enqueue_manual_run"} == set(
        _OWNER_SEAMS
    )


def test_retired_routes_have_only_unconditional_gone_outcomes() -> None:
    for (method, path), code in RETIRED_RESOURCE_ROUTES.items():
        route = next(
            route for route in app.routes if route.path == path and method in route.methods
        )
        endpoint = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
        calls = [node for node in ast.walk(endpoint) if isinstance(node, ast.Call)]
        assert len(calls) == 2  # router registration and the unconditional GoneError
        assert code in inspect.getsource(route.endpoint)
        assert any(isinstance(node, ast.Raise) for node in ast.walk(endpoint))
        assert not any(
            isinstance(node, ast.Await | ast.If | ast.Return) for node in ast.walk(endpoint)
        )


def test_new_non_route_seams_are_explicitly_classified() -> None:
    for owner, method, action in DIRECT_RESOURCE_SERVICE_GUARDS:
        operation = getattr(_OWNER_SEAMS[owner], method)
        assert operation.__denial_action__ == action
        assert "run_guarded" in operation.__code__.co_names
        assert inspect.getclosurevars(operation).nonlocals["missing_result"]
    assert set(NON_RESOURCE_DENIAL_SEAMS) == {
        "DocumentUploadService.audit_rejection",
        "app.ingestion.reembed._main",
        "app.tasks.upload_janitor.sweep_expired_uploads_async",
        "app.services.tools.gate.PolicyApprovalGate",
        "app.services.tools.runner.ToolRunner",
    }
    assert all(NON_RESOURCE_DENIAL_SEAMS.values())


def test_every_declared_owner_has_a_mandatory_construction_seam() -> None:
    """Owners cannot silently return an unaudited 404 through an optional argument."""
    for owner, seam in _OWNER_SEAMS.items():
        parameter = inspect.signature(seam).parameters.get("denials")
        assert parameter is not None, f"{owner} omits the canonical denial context"
        assert parameter.default is inspect.Parameter.empty
        annotation = get_type_hints(
            inspect.unwrap(seam.__init__ if inspect.isclass(seam) else seam)
        )["denials"]
        assert annotation in (PermissionDeniedContext, PermissionDeniedContext | None)


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/api/v1/documents/{future_id}/rotate"),
        ("GET", "/api/v1/code-runs/{future_id}/future"),
    ],
)
def test_manifest_rejects_future_authenticated_target(method, path, monkeypatch) -> None:
    # A failure in the existing registry must not satisfy this negative test.
    test_direct_resource_guard_manifest_matches_the_registered_api()

    async def endpoint():
        return {}

    route = APIRoute(path, endpoint, methods=[method], dependencies=[Depends(current_user)])
    assert _registered_direct_routes([route]) == {(method, path)}, f"{method} {path} not discovered"
    monkeypatch.setattr(app.router, "routes", [*app.routes, route])
    with pytest.raises(AssertionError) as failure:
        test_direct_resource_guard_manifest_matches_the_registered_api()
    assert path in str(failure.value), f"manifest failure does not name {path}: {failure.value}"


def test_future_rest_target_check_rejects_an_omitted_family(monkeypatch) -> None:
    """An existing code-run mismatch cannot satisfy the future-route check."""
    test_direct_resource_guard_manifest_matches_the_registered_api()
    discover = _registered_direct_routes

    def omit_code_runs(routes):
        return {
            target for target in discover(routes) if not target[1].startswith("/api/v1/code-runs/")
        }

    monkeypatch.setattr(sys.modules[__name__], "_registered_direct_routes", omit_code_runs)
    with pytest.raises(AssertionError, match="extra=.*code-runs"):
        test_manifest_rejects_future_authenticated_target(
            "GET", "/api/v1/code-runs/{future_id}/future", monkeypatch
        )


def test_future_rest_target_check_requires_discovery(monkeypatch) -> None:
    """A healthy baseline cannot mask failure to discover only the new target."""
    test_direct_resource_guard_manifest_matches_the_registered_api()
    path = "/api/v1/code-runs/{future_id}/future"
    discover = _registered_direct_routes

    def omit_future_target(routes):
        return {target for target in discover(routes) if target != ("GET", path)}

    monkeypatch.setattr(sys.modules[__name__], "_registered_direct_routes", omit_future_target)
    with pytest.raises(AssertionError, match="not discovered"):
        test_manifest_rejects_future_authenticated_target("GET", path, monkeypatch)


def test_future_rest_target_check_rejects_an_unrelated_failure(monkeypatch) -> None:
    """After injection, only an error naming the synthetic target is evidence."""
    test_direct_resource_guard_manifest_matches_the_registered_api()
    path = "/api/v1/code-runs/{future_id}/future"
    check_manifest = test_direct_resource_guard_manifest_matches_the_registered_api

    def unrelated_failure():
        if any(route.path == path for route in app.routes):
            raise AssertionError("unrelated existing-route mismatch")
        check_manifest()

    monkeypatch.setattr(
        sys.modules[__name__],
        "test_direct_resource_guard_manifest_matches_the_registered_api",
        unrelated_failure,
    )
    with pytest.raises(AssertionError, match="does not name"):
        test_manifest_rejects_future_authenticated_target("GET", path, monkeypatch)


@pytest.mark.parametrize(
    "method,path", [("GET", "/api/v1/future-resources"), ("POST", "/api/v1/future-resources")]
)
def test_manifest_ignores_non_target_list_and_create(method, path, monkeypatch) -> None:
    async def endpoint():
        return {}

    route = APIRoute(path, endpoint, methods=[method], dependencies=[Depends(current_user)])
    monkeypatch.setattr(app.router, "routes", [*app.routes, route])
    test_direct_resource_guard_manifest_matches_the_registered_api()


def test_legacy_raw_recorder_owner_fails_typed_ownership_gate(monkeypatch) -> None:
    class LegacyGuard:
        def __init__(self, *, denials: PermissionDeniedRecorder, actor, request_id, source_ip):
            pass

    monkeypatch.setitem(_OWNER_SEAMS, "LegacyGuard", LegacyGuard)
    with pytest.raises(AssertionError):
        test_every_declared_owner_has_a_mandatory_construction_seam()


def test_chat_websocket_target_has_a_declared_audit_owner() -> None:
    assert any(
        entry.method == "WEBSOCKET" and entry.path == "/ws/chat/{stream_id}"
        for entry in DIRECT_RESOURCE_DENIAL_GUARDS
    )


@pytest.mark.parametrize("inline_auth", [False, True])
def test_future_websocket_target_is_explicitly_discovered(inline_auth) -> None:
    async def endpoint():
        pass

    route = APIWebSocketRoute(
        "/ws/future/{resource_id}",
        endpoint,
        dependencies=[] if inline_auth else [Depends(current_user)],
    )
    assert _registered_direct_routes([route]) == {("WEBSOCKET", "/ws/future/{resource_id}")}


@pytest.mark.parametrize("inline_auth", [False, True])
def test_manifest_rejects_future_authenticated_websocket_target(monkeypatch, inline_auth) -> None:
    # A failure in the existing registry must not satisfy this negative test.
    test_direct_resource_guard_manifest_matches_the_registered_api()

    async def endpoint():
        pass

    route = APIWebSocketRoute(
        "/ws/future/{resource_id}",
        endpoint,
        dependencies=[] if inline_auth else [Depends(current_user)],
    )
    assert ("WEBSOCKET", route.path) in _registered_direct_routes([route])
    monkeypatch.setattr(app.router, "routes", [*app.routes, route])
    with pytest.raises(AssertionError) as failure:
        test_direct_resource_guard_manifest_matches_the_registered_api()
    assert "/ws/future/{resource_id}" in str(failure.value)


def test_every_manifest_action_has_an_audited_terminal_service_seam() -> None:
    """The whole operation, including late repository misses, passes one helper.

    This checks actual callable wrappers rather than counting emit calls inside
    initial SELECT guards. Removing a wrapper exposes every raw return/raise
    beneath it, and must fail even when the constructor still takes denials.
    """
    for entry in DIRECT_RESOURCE_DENIAL_GUARDS:
        seam = _OWNER_SEAMS[entry.owner]
        operations = (
            inspect.getmembers(seam, inspect.isfunction) if inspect.isclass(seam) else [("", seam)]
        )
        guarded = [
            operation
            for _, operation in operations
            if getattr(operation, "__denial_action__", None) == entry.attempted_action
        ]
        assert guarded, f"{entry.owner}: raw terminal outcome for {entry.attempted_action}"
        for operation in guarded:
            assert "run_guarded" in operation.__code__.co_names
            closure = inspect.getclosurevars(operation).nonlocals
            assert closure["operation"] is operation.__wrapped__
            result = get_type_hints(inspect.unwrap(operation)).get("return")
            # These two None results are successful, documented domain values.
            nullable_success = (entry.owner, operation.__name__) in {
                ("SandboxSessionService", "get"),
                ("GroupsService", "member_count"),
            }
            if (result is bool or type(None) in get_args(result)) and not nullable_success:
                assert closure["missing_result"], (entry, operation.__name__)
        # Check the actual route entry point calls a protected service operation,
        # rather than accepting an unused method with a matching marker.
        route = next(
            route
            for route in app.routes
            if route.path == entry.path
            and (entry.method == "WEBSOCKET" or entry.method in getattr(route, "methods", set()))
        )
        endpoint = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
        calls = {
            node.func.attr
            for node in ast.walk(endpoint)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert calls & {operation.__name__ for operation in guarded}, (entry, calls)
        direct_calls = {
            node.func.attr
            for node in ast.walk(endpoint)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        }
        for name, operation in operations:
            if f"{entry.owner}.{name}" in NON_RESOURCE_DENIAL_SEAMS:
                # The media error-evidence sink takes an id for attribution,
                # but does not resolve a target or return a denial.
                assert get_type_hints(operation)["return"] is type(None)
                assert "PERMISSION_DENIED" not in inspect.getsource(operation)
                continue
            if name in direct_calls and any(
                parameter.endswith("_id") for parameter in inspect.signature(operation).parameters
            ):
                assert "run_guarded" in operation.__code__.co_names, (entry, name)


def test_terminal_seam_registry_rejects_a_raw_not_found_service(monkeypatch) -> None:
    test_every_manifest_action_has_an_audited_terminal_service_seam()

    class RawService:
        async def update(self, resource_id):
            return None

    monkeypatch.setitem(_OWNER_SEAMS, "CollectionsService", RawService)
    with pytest.raises(AssertionError, match="CollectionsService: raw terminal outcome"):
        test_every_manifest_action_has_an_audited_terminal_service_seam()
