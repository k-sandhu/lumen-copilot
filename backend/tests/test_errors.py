"""Unit tests for the typed error hierarchy + problem-detail mapper.

Pure unit tests (no app, no I/O): assert that ``AppError`` subclasses render the
``Problem`` shape from ``contracts/openapi.yaml`` and that an unknown exception
is coerced to an opaque 500 that never leaks the original message.
"""

from __future__ import annotations

from app.core.errors import (
    AppError,
    FieldError,
    ForbiddenError,
    NotFoundError,
    Problem,
    ValidationError,
    problem_from_exception,
)


def test_app_error_renders_declared_problem_shape() -> None:
    exc = NotFoundError("widget 42 does not exist")
    problem = exc.to_problem(instance="/api/v1/widgets/42")

    assert isinstance(problem, Problem)
    assert problem.status == 404
    assert problem.code == "not_found"
    assert problem.title == "Not Found"
    assert problem.detail == "widget 42 does not exist"
    assert problem.instance == "/api/v1/widgets/42"


def test_problem_from_known_app_error() -> None:
    status_code, body = problem_from_exception(ForbiddenError("wrong tenant"))

    assert status_code == 403
    assert body["status"] == 403
    assert body["code"] == "forbidden"
    assert body["detail"] == "wrong tenant"
    # None fields are omitted from the wire body.
    assert "errors" not in body


def test_problem_from_unknown_exception_is_opaque_500() -> None:
    secret = "DB password leaked in message: hunter2"
    status_code, body = problem_from_exception(RuntimeError(secret))

    assert status_code == 500
    assert body["status"] == 500
    assert body["code"] == "internal_error"
    # The original message must NOT appear anywhere in the client payload.
    assert secret not in str(body)
    assert body["title"] == "Internal Server Error"


def test_validation_error_carries_field_errors() -> None:
    exc = ValidationError(
        "bad input",
        field_errors=[FieldError(field="email", message="not an email")],
    )
    problem = exc.to_problem()

    assert problem.status == 422
    assert problem.errors is not None
    assert problem.errors[0].field == "email"


def test_app_error_overrides_apply() -> None:
    exc = AppError("boom", title="Teapot", code="teapot", status=418)
    problem = exc.to_problem()

    assert (problem.status, problem.title, problem.code) == (418, "Teapot", "teapot")
