"""Worker-qualified names for explicitly disposable PostgreSQL test databases."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse, urlunparse


def worker_database_name(name: str, worker: str | None = None) -> str:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "") if worker is None else worker
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("test database name must be a safe PostgreSQL identifier")
    if worker and not re.fullmatch(r"gw[0-9]+", worker):
        raise ValueError("unexpected xdist worker identifier")
    suffix = f"_{worker}" if worker else ""
    result = name if suffix and name.endswith(suffix) else name + suffix
    if len(result) > 63:
        raise ValueError("test database name exceeds PostgreSQL's identifier limit")
    return result


def worker_database_url(url: str) -> str:
    parsed = urlparse(url)
    name = parsed.path.removeprefix("/")
    return urlunparse(parsed._replace(path="/" + worker_database_name(name)))


def isolated_live_url(default: str, env_var: str = "DATABASE_URL") -> str:
    """Allow a caller-owned lumentest database; never select the app database."""
    supplied = os.environ.get(env_var, "")
    if supplied and urlparse(supplied).path.startswith("/lumentest_"):
        default = supplied
    if not urlparse(default).path.startswith("/lumentest_"):
        raise ValueError("live database must be explicitly disposable (lumentest_ prefix)")
    return worker_database_url(default)
