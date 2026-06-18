"""API test for the liveness probe.

Asserts ``GET /health`` returns 200 and the exact ``HealthStatus`` shape from
``contracts/openapi.yaml`` (``status`` == ``"ok"``, plus ``service`` and
``version``). External dependencies are mocked: the lifespan's ``ensure_bucket``
is stubbed so the app boots without a live MinIO, and ``/health`` itself does no
I/O. The compose healthcheck relies on this endpoint, so its shape is
load-bearing.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient whose startup does not reach out to MinIO."""
    # Patch the lifespan's bucket bootstrap so no network is required to boot.
    with patch("app.main.get_object_store") as get_store:
        store = get_store.return_value
        store.ensure_bucket = AsyncMock(return_value=None)
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client


def test_health_returns_ok_and_contract_shape(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    # Exactly the HealthStatus contract shape (additionalProperties: false).
    assert set(body.keys()) == {"status", "service", "version"}
    assert body["status"] == "ok"
    assert body["service"] == "beacon-backend"
    assert isinstance(body["version"], str) and body["version"]


def test_unknown_route_is_not_found(client: TestClient) -> None:
    # Negative test: an unmapped path returns 404 (FastAPI default), not a 5xx.
    response = client.get("/nope")
    assert response.status_code == 404
