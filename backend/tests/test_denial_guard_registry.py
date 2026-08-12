"""Mechanical completeness checks for authenticated direct-resource denials (#579)."""

from __future__ import annotations

import inspect

from app.api.denial_registry import (
    DIRECT_RESOURCE_DENIAL_GUARDS,
    EXPLICIT_GUARDED_NON_TARGET_ROUTES,
    GOVERNED_DENIAL_PREFIXES,
)
from app.main import app
from app.services.admin_service import AdminService
from app.services.artifacts_service import ArtifactsService
from app.services.connector_oauth_service import ConnectorOAuthService
from app.services.document_service import DocumentService
from app.services.groups_service import GroupsService
from app.services.llm_providers_service import LlmProviderService
from app.services.mcp_servers_service import McpServersService
from app.services.run_delivery_service import RunDeliveryService
from app.services.saved_searches_service import SavedSearchService
from app.services.sources_service import SourcesService


_OWNER_SEAMS = {
    "DocumentService": DocumentService,
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


def test_direct_resource_guard_manifest_matches_the_registered_api() -> None:
    """Every confirmed family has one named owner/action and no unregistered entry."""
    manifest = {(entry.method, entry.path): entry for entry in DIRECT_RESOURCE_DENIAL_GUARDS}
    assert len(manifest) == len(DIRECT_RESOURCE_DENIAL_GUARDS), "duplicate guard route"
    assert all(entry.owner and entry.attempted_action for entry in manifest.values())

    registered = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
        if method in {"GET", "POST", "PATCH", "DELETE"}
    }
    # A new direct-id route in one of these confirmed families cannot silently
    # bypass the inventory: the live registry must match the declarations exactly.
    # Non-target list/create operations do not get swept in; the two confirmed
    # non-path guards are explicit production declarations.
    registered_direct = {
        key for key in registered if "{" in key[1] and key[1].startswith(GOVERNED_DENIAL_PREFIXES)
    }
    governed_registered = registered_direct | EXPLICIT_GUARDED_NON_TARGET_ROUTES
    assert set(manifest) == governed_registered
    assert set(manifest) <= registered
    assert {entry.owner for entry in manifest.values()} == set(_OWNER_SEAMS)


def test_every_declared_owner_has_a_mandatory_construction_seam() -> None:
    """Owners cannot silently return an unaudited 404 through an optional argument."""
    for owner, seam in _OWNER_SEAMS.items():
        parameter = inspect.signature(seam).parameters.get("denials")
        assert parameter is not None, f"{owner} omits the canonical denial context"
        assert parameter.default is inspect.Parameter.empty
