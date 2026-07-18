"""Web-search config guardrails (ADR-0014, issue #219).

Pin the ``web_search`` settings: off-by-default (governance, ADR-0014 §5), the
sane defaults, and the fail-fast validators (a non-positive k/window/limit or a
negative fetch-top-n must refuse to boot rather than disable bounding).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings

_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://t:t@localhost/t",
    "REDIS_URL": "redis://localhost",
    "CELERY_BROKER_URL": "redis://localhost",
    "CELERY_RESULT_BACKEND": "redis://localhost",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "t",
    "S3_SECRET_KEY": "tt",
    "S3_BUCKET": "b",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **_BASE, **overrides)  # type: ignore[arg-type]


def test_web_search_is_off_by_default() -> None:
    """AC-3 posture: web search is disabled unless a deploy opts in (ADR-0014 §5)."""
    assert _settings().web_search_enabled is False


def test_web_search_defaults() -> None:
    s = _settings()
    assert s.web_search_default_k == 5
    assert s.web_search_max_k == 10
    assert s.web_search_fetch_top_n == 3
    assert s.web_search_rate_max_per_window == 20
    assert s.web_search_rate_window_seconds == 60
    # The endpoint default targets the mapped host port so host-side dev/tests
    # reach the same engine compose runs (mirrors the OpenSearch pattern).
    assert s.web_search_endpoint == "http://localhost:47187"


def test_web_search_enable_flag_from_env() -> None:
    assert _settings(WEB_SEARCH_ENABLED=True).web_search_enabled is True


def test_compose_runtime_services_use_internal_searxng_endpoint() -> None:
    """Compose callers reach SearXNG by service name, with an override escape hatch.

    The Settings default intentionally targets the published host port for a
    backend launched directly on the host.  Inside Compose, however,
    ``localhost`` is the backend/worker container itself.  Both services that can
    run chat turns must therefore override that host-side default with the
    Compose-network address while preserving an explicit deployment override.
    """
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    expected = "${WEB_SEARCH_ENDPOINT:-http://searxng:8080}"

    for service_name in ("backend", "worker"):
        environment = compose["services"][service_name]["environment"]
        assert environment["WEB_SEARCH_ENDPOINT"] == expected


def _resolved_compose_web_search_endpoints(
    tmp_path: Path, *, override: str | None
) -> dict[str, str]:
    """Resolve Compose config without starting or mutating any Docker resources."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")

    compose_version = subprocess.run(
        [docker, "compose", "version"],
        capture_output=True,
        check=False,
        text=True,
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose plugin is unavailable")

    # ``env_file: .env`` is resolved relative to --project-directory. A temporary
    # project directory makes the test independent of any developer .env while
    # keeping all generated input under pytest's automatic cleanup.
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "POSTGRES_USER=test",
                "POSTGRES_PASSWORD=test-password",
                "POSTGRES_DB=test",
                "MINIO_ROOT_USER=test",
                "MINIO_ROOT_PASSWORD=test-password",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment.pop("WEB_SEARCH_ENDPOINT", None)
    if override is not None:
        environment["WEB_SEARCH_ENDPOINT"] = override

    root = Path(__file__).resolve().parents[2]
    resolved = subprocess.run(
        [
            docker,
            "compose",
            "--project-directory",
            str(tmp_path),
            "-f",
            str(root / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert resolved.returncode == 0, resolved.stderr
    services = json.loads(resolved.stdout)["services"]
    return {
        service_name: services[service_name]["environment"]["WEB_SEARCH_ENDPOINT"]
        for service_name in ("backend", "worker")
    }


def test_compose_resolves_internal_searxng_endpoint_when_unset(tmp_path: Path) -> None:
    assert _resolved_compose_web_search_endpoints(tmp_path, override=None) == {
        "backend": "http://searxng:8080",
        "worker": "http://searxng:8080",
    }


def test_compose_resolves_explicit_web_search_endpoint_override(tmp_path: Path) -> None:
    override = "https://search.override.test"
    assert _resolved_compose_web_search_endpoints(tmp_path, override=override) == {
        "backend": override,
        "worker": override,
    }


@pytest.mark.parametrize(
    "field",
    [
        "WEB_SEARCH_DEFAULT_K",
        "WEB_SEARCH_MAX_K",
        "WEB_SEARCH_RATE_MAX_PER_WINDOW",
        "WEB_SEARCH_RATE_WINDOW_SECONDS",
    ],
)
def test_non_positive_counts_fail_fast(field: str) -> None:
    """A zero/negative k or rate window/limit disables bounding — must not boot."""
    with pytest.raises(ValidationError):
        _settings(**{field: 0})


def test_negative_fetch_top_n_fails_fast() -> None:
    with pytest.raises(ValidationError):
        _settings(WEB_SEARCH_FETCH_TOP_N=-1)


def test_zero_fetch_top_n_is_allowed_snippets_only() -> None:
    """``0`` is a valid config: snippets only, no result-page fetch."""
    assert _settings(WEB_SEARCH_FETCH_TOP_N=0).web_search_fetch_top_n == 0
