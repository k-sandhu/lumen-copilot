from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from lumen_sandbox_runner.auth import TOKEN_ENV, TOKEN_HEADER
from lumen_sandbox_runner.main import app

#: These exercise request VALIDATION, which now sits behind authentication (#508),
#: so they authenticate. The auth gate itself is pinned in `test_auth.py`.
_TOKEN = "t" * 48
_AUTH = {TOKEN_HEADER: _TOKEN}


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, _TOKEN)


async def test_health_and_security_validation_are_available_without_docker() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        response = await client.put(
            "/sessions/00000000-0000-0000-0000-000000000001",
            headers=_AUTH,
            json={
                "generation": 1,
                "image": "python",
                "env": {},
                "container": {"network_mode": "bridge"},
            },
        )
        assert response.status_code == 422


async def test_package_requirement_length_is_rejected_before_engine_access() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/00000000-0000-0000-0000-000000000001/executions",
            headers=_AUTH,
            json={
                "generation": 1,
                "execution_id": "00000000-0000-0000-0000-000000000002",
                "code": "print('unreachable')",
                "packages": ["a" * 301],
            },
        )

    assert response.status_code == 422
