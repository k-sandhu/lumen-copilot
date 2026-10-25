"""Exception logs remain renderable under strict warnings (#803)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
import structlog

from app.core.logging import configure_logging


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    previous = structlog.get_config()
    yield
    structlog.configure(**previous)


@pytest.mark.parametrize("json_logs", [True, False], ids=["json", "console"])
def test_exception_logging_does_not_warn(
    json_logs: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(json_logs=json_logs)
    logger = structlog.get_logger("strict-exception-regression")
    synthetic_secret = "must-not-expose-local-secret"
    try:
        raise ValueError("synthetic failure")
    except ValueError:
        logger.exception("operation.failed")
    output = capsys.readouterr().out
    assert "operation.failed" in output
    assert "ValueError" in output
    assert synthetic_secret not in output
    if json_logs:
        assert "synthetic failure" in json.loads(output)["exception"]
