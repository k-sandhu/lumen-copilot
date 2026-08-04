"""Sandbox config tests — default-OFF + fail-fast validators (ADR-0013 §2/§6, #230).

The sandbox is the highest-risk capability, so its config must fail closed: code
execution is **disabled by default** (the kill-switch), and every load-bearing
resource cap / quota must be positive (a non-positive value would disable an
isolation control) — a misconfiguration refuses to boot rather than launching runs
unbounded.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from tests._sandbox_helpers import sandbox_settings


def test_sandbox_disabled_by_default() -> None:
    """The kill-switch: code execution is OFF for every tenant until explicitly enabled."""
    settings = sandbox_settings(SANDBOX_ENABLED="false")
    assert settings.sandbox_enabled is False


def test_sandbox_default_omitted_is_off() -> None:
    """With no override at all, ``sandbox_enabled`` defaults to False (ADR-0013 §6)."""
    from app.core.config import Settings

    settings = Settings(  # type: ignore[call-arg]
        DATABASE_URL="sqlite+aiosqlite://",
        REDIS_URL="redis://localhost:6379/0",
        CELERY_BROKER_URL="redis://localhost:6379/1",
        CELERY_RESULT_BACKEND="redis://localhost:6379/2",
        S3_ENDPOINT_URL="http://localhost:9000",
        S3_ACCESS_KEY="k",
        S3_SECRET_KEY="s",
        S3_BUCKET="b",
        OPENROUTER_API_KEY="",
    )
    assert settings.sandbox_enabled is False


@pytest.mark.parametrize(
    "field",
    [
        "SANDBOX_CPUS",
        "SANDBOX_MEMORY_BYTES",
        "SANDBOX_PIDS_LIMIT",
        "SANDBOX_WALL_CLOCK_SECONDS",
        "SANDBOX_OUTPUT_BYTES_CAP",
        "SANDBOX_SCRATCH_BYTES",
        "SANDBOX_MAX_CONCURRENT_PER_TENANT",
        "SANDBOX_DAILY_RUNTIME_SECONDS_PER_TENANT",
    ],
)
def test_non_positive_limit_or_quota_rejected(field: str) -> None:
    """A zero/negative resource cap or quota would disable an isolation control — reject."""
    with pytest.raises(ValueError, match="must be positive"):
        sandbox_settings(**{field: "0"})


def test_unknown_runtime_rejected() -> None:
    """Only ``runc`` (Docker baseline) or ``runsc`` (gVisor) are valid runtimes."""
    with pytest.raises(ValueError, match="runc.*runsc|runsc"):
        sandbox_settings(SANDBOX_RUNTIME="firecracker")


@pytest.mark.parametrize("runtime", ["runc", "runsc"])
def test_known_runtimes_accepted(runtime: str) -> None:
    """gVisor is a config swap — both runtimes construct cleanly."""
    settings = sandbox_settings(SANDBOX_RUNTIME=runtime)
    assert settings.sandbox_runtime == runtime


# --- The image the tenant code actually runs in is pinned at LAUNCH time ------
#
# ADR-0013 §3 asks for an execution image "pinned by digest, no ``:latest``". The
# ``sandbox_exec/Dockerfile`` FROM line pins its BASE by digest, but that is a
# BUILD-time fact about a layer; the reference the runner launches comes from
# ``SANDBOX_IMAGE`` at run time. Until these validators existed that value took
# ``:latest``, a tagless name, or a remote ref unchallenged — so the documented
# guarantee applied to the base layer while the thing model code executes in was a
# mutable tag.

_DIGEST = "sha256:" + "a" * 64


def test_default_execution_image_is_tag_pinned_and_not_latest() -> None:
    """The shipped default names an exact tag — never a floating one."""
    image = sandbox_settings().sandbox_image

    assert ":latest" not in image
    assert ":" in image.rsplit("/", 1)[-1]


@pytest.mark.parametrize(
    "image",
    [
        "lumen-sandbox-exec:latest",
        "lumen-sandbox-exec",
        "ghcr.io/lumen/lumen-sandbox-exec",
        "registry.internal:5000/lumen-sandbox-exec",
        "lumen-sandbox-exec@sha256:abc",
    ],
)
def test_floating_or_malformed_execution_image_is_rejected(image: str) -> None:
    """A mutable or unparseable reference refuses to boot rather than run code."""
    with pytest.raises(ValueError, match="SANDBOX_IMAGE"):
        sandbox_settings(SANDBOX_IMAGE=image)


@pytest.mark.parametrize(
    "image",
    [f"lumen-sandbox-exec@{_DIGEST}", f"lumen-sandbox-exec:0.1.1@{_DIGEST}"],
)
def test_digest_pinned_execution_image_is_accepted(image: str) -> None:
    """``name@sha256:…`` (with or without a readability tag) is the strongest pin."""
    assert sandbox_settings(SANDBOX_IMAGE=image).sandbox_image == image


def _production_env(**overrides: object) -> dict[str, object]:
    """The minimal non-local boot env (see test_sandbox_isolation for the pedigree)."""
    base: dict[str, object] = {
        "ENVIRONMENT": "production",
        "JWT_SECRET": "production-secret-that-is-not-the-dev-default",
        "SECRETS_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "CONNECTOR_OAUTH_REDIRECT_BASE_URL": "https://api.example.com",
        "CONNECTOR_OAUTH_FRONTEND_RETURN_URL": "https://app.example.com/sources",
        "GDRIVE_OAUTH_CLIENT_ID": "prod-google-client-id",
        "GDRIVE_OAUTH_CLIENT_SECRET": "prod-google-client-secret",
        "SANDBOX_RUNTIME": "runsc",
    }
    base.update(overrides)
    return base


def test_enabled_sandbox_outside_local_requires_a_digest_pinned_image() -> None:
    """A tag is trust-on-first-build; outside local dev the digest is mandatory."""
    with pytest.raises(ValueError, match="@sha256"):
        sandbox_settings(**_production_env(SANDBOX_IMAGE="lumen-sandbox-exec:0.1.1"))

    settings = sandbox_settings(**_production_env(SANDBOX_IMAGE=f"lumen-sandbox-exec@{_DIGEST}"))
    assert settings.sandbox_image.endswith(_DIGEST)


def test_disabled_sandbox_outside_local_still_boots_on_the_tag_default() -> None:
    """The digest requirement follows the capability, not the environment alone.

    A deploy with code execution OFF launches nothing, so holding its boot hostage
    to an image reference it never uses would be a gratuitous outage.
    """
    settings = sandbox_settings(**_production_env(SANDBOX_ENABLED="false"))

    assert settings.sandbox_enabled is False
    assert settings.sandbox_image == "lumen-sandbox-exec:0.1.1"


# --- The pre-installed manifest must be settable from the ENVIRONMENT ---------
#
# Not a style point. ``pydantic-settings`` JSON-decodes a complex-typed field's env
# value inside ``EnvSettingsSource``, BEFORE field validators run, so the
# ``mode="before"`` comma splitter never saw env input: every documented form raised
# ``SettingsError``. The empty form was the shipped one — ``.env.example`` carried a
# commented ``SANDBOX_PREINSTALLED_PACKAGES=``, and an operator who copied the file and
# uncommented that line broke the API *and* the worker at boot. These tests go through
# real environment variables (``sandbox_settings(**kwargs)`` uses the INIT source and
# cannot reproduce the defect).

_ENV_BOOT_MINIMUM = {
    "DATABASE_URL": "sqlite+aiosqlite://",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "lumen",
    "S3_SECRET_KEY": "lumen_local_dev_secret",
    "S3_BUCKET": "b",
    "OPENROUTER_API_KEY": "",
    "SANDBOX_ENABLED": "true",
}


def _settings_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Construct ``Settings`` from the ENV source alone (``.env`` deliberately off)."""
    for key, value in {**_ENV_BOOT_MINIMUM, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_preinstalled_manifest_parses_the_documented_comma_form_from_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The form the config docstring, ``.env.example`` and the runbook all document."""
    settings = _settings_from_env(
        monkeypatch, SANDBOX_PREINSTALLED_PACKAGES="numpy==1.0, pandas==2.0"
    )

    assert settings.sandbox_preinstalled_packages == ("numpy==1.0", "pandas==2.0")


def test_a_single_pin_from_the_env_is_not_mistaken_for_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One entry has no comma at all — the JSON decoder choked on this too."""
    settings = _settings_from_env(monkeypatch, SANDBOX_PREINSTALLED_PACKAGES="numpy==1.0")

    assert settings.sandbox_preinstalled_packages == ("numpy==1.0",)


def test_an_empty_manifest_value_boots_instead_of_killing_the_api_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact regression: the empty assignment must not be a boot failure.

    It now means what it reads as — "this image ships nothing" — so every
    ``packages=[...]`` request is refused. That is fail-closed and recoverable; a
    ``SettingsError`` at import was neither.
    """
    settings = _settings_from_env(monkeypatch, SANDBOX_PREINSTALLED_PACKAGES="")

    assert settings.sandbox_preinstalled_packages == ()


def test_unset_manifest_keeps_the_shipped_image_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent the variable, config still mirrors ``sandbox_exec/requirements.txt``."""
    from app.core.config import _DEFAULT_SANDBOX_PREINSTALLED_PACKAGES

    monkeypatch.delenv("SANDBOX_PREINSTALLED_PACKAGES", raising=False)
    settings = _settings_from_env(monkeypatch)

    assert settings.sandbox_preinstalled_packages == _DEFAULT_SANDBOX_PREINSTALLED_PACKAGES


# --- #511: the same env-decode trap, on every complex-typed setting -------------


@pytest.mark.parametrize(
    ("variable", "comma_form", "expected"),
    [
        (
            "UPLOAD_ALLOWED_CONTENT_TYPES",
            "application/pdf, text/plain",
            frozenset({"application/pdf", "text/plain"}),
        ),
        (
            "ARTIFACT_ALLOWED_CONTENT_TYPES",
            "text/csv,image/png",
            frozenset({"text/csv", "image/png"}),
        ),
        (
            "LOGO_ALLOWED_CONTENT_TYPES",
            "image/png",
            frozenset({"image/png"}),
        ),
        (
            "MCP_ALLOWED_TRANSPORTS",
            "sse, streamable_http",
            frozenset({"sse", "streamable_http"}),
        ),
        (
            "MCP_ENDPOINT_ALLOWLIST",
            "https://a.example,https://b.example",
            frozenset({"https://a.example", "https://b.example"}),
        ),
    ],
)
def test_every_complex_setting_accepts_its_documented_comma_form_from_the_env(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    comma_form: str,
    expected: frozenset[str],
) -> None:
    """#511: the trap was never sandbox-specific, it was inherited by five settings.

    ``pydantic-settings`` JSON-decodes a complex-typed env value inside
    ``EnvSettingsSource`` — BEFORE any validator runs — so a ``mode="before"``
    splitter is dead code for env input and the documented comma form raises
    ``SettingsError`` at import. #507 fixed the sandbox instance with ``NoDecode``;
    every sibling with the same shape was still broken, which is to say **the
    documented way to configure uploads, artifacts, branding and MCP did not work
    at all** and took the process down at boot rather than failing visibly.

    Parametrised deliberately: the next complex-typed setting someone adds should
    show up here as a missing row, not as a support ticket.
    """
    settings = _settings_from_env(monkeypatch, **{variable: comma_form})

    assert getattr(settings, variable.lower()) == expected


def test_an_empty_complex_setting_means_empty_rather_than_crashing_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst case of #511: a copied `.env` with a blank line took the app down.

    Blank now means what it says — an empty allow-list, which for uploads is a
    deny-everything posture the operator chose. It is not a decode failure that
    prevents the API and the worker from starting.
    """
    settings = _settings_from_env(monkeypatch, UPLOAD_ALLOWED_CONTENT_TYPES="")

    assert settings.upload_allowed_content_types == frozenset()


def test_a_blank_model_registry_falls_back_to_the_default_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CHAT_MODEL_REGISTRY`'s documented blank fallback was unreachable (#511).

    Its validator has always claimed "a blank string falls back to the default
    seed", but the JSON decode happened first, so blank raised `SettingsError`
    before that branch could run. The JSON form keeps working — this is additive.
    """
    settings = _settings_from_env(monkeypatch, CHAT_MODEL_REGISTRY="")

    assert len(settings.chat_model_registry) >= 1
    assert sum(1 for m in settings.chat_model_registry if m.is_default) == 1


def test_the_registry_still_takes_its_documented_json_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the above: NoDecode must not break the JSON that works."""
    settings = _settings_from_env(
        monkeypatch,
        CHAT_MODEL_REGISTRY=(
            '[{"id": "openrouter/x/y", "label": "Y", "provider": "openrouter", '
            '"tier": "frontier", "is_default": true}]'
        ),
    )

    assert [m.id for m in settings.chat_model_registry] == ["openrouter/x/y"]


# --- #520: the image reference must be a reference, not merely un-mutable -------

_DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "reference",
    [
        f"@{_DIGEST}",  # empty name
        "repo name:tag",  # embedded space
        ":tag",  # empty name segment
        "/name:1.0",  # empty leading segment
        "repo:tag:extra",  # second colon is not a tag
        "name:-badtag",  # a tag may not start with '-'
        "UPPER/name:1.0",  # a path component must be lowercase…
        "Name:1.0",  # …including the only one
    ],
)
def test_a_malformed_reference_is_refused_at_boot(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    """#520: "not `:latest`" and "has a digest" say nothing about being parseable.

    Each of these was accepted by the earlier validator. They all fail closed
    eventually — the runner cannot resolve them — but "the daemon rejected your
    image" at the first execution is a far worse error than "your config is
    malformed" at boot, and it surfaces to a user mid-task rather than to the
    operator mid-deploy.
    """
    with pytest.raises(ValueError, match="SANDBOX_IMAGE"):
        _settings_from_env(monkeypatch, SANDBOX_IMAGE=reference)


@pytest.mark.parametrize(
    "reference",
    [
        "lumen-sandbox-exec:0.1.1",
        f"name@{_DIGEST}",
        "registry.internal:5000/name:1.0",  # a registry PORT is not a tag
        f"registry.internal:5000/team/name@{_DIGEST}",
        "ghcr.io/org/sub_path.name:v1.2.3-rc1",
        "localhost:5000/name:1.0",
        "localhost/name:1.0",
    ],
)
def test_a_well_formed_reference_is_accepted(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    settings = _settings_from_env(monkeypatch, SANDBOX_IMAGE=reference)

    assert settings.sandbox_image == reference


def test_a_digest_pinned_latest_is_immutable_and_must_not_be_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#520's false rejection — and it punished the most explicit possible pin.

    `:latest` is mutable only when it is what RESOLVES the image. With a digest
    present the daemon resolves by digest and the tag is a human-readable label, so
    `name:latest@sha256:…` is exactly as immutable as `name@sha256:…`. It is also
    the form `docker pull` echoes back, so an operator copying what Docker printed
    hit a boot failure telling them to stop using a tag they had already pinned.
    """
    reference = f"lumen-sandbox-exec:latest@{_DIGEST}"

    settings = _settings_from_env(monkeypatch, SANDBOX_IMAGE=reference)

    assert settings.sandbox_image == reference


def test_a_bare_latest_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: relaxing the digest case must not relax the mutable one."""
    with pytest.raises(ValueError, match="latest"):
        _settings_from_env(monkeypatch, SANDBOX_IMAGE="lumen-sandbox-exec:latest")
