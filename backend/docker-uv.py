#!/usr/bin/env python3
"""uv container entry point: refuse host paths before uv can create anything."""

import os
import posixpath
import sys


def valid_environment(path: str) -> bool:
    return (
        path.startswith("/tmp/")
        and posixpath.normpath(path) == path
        and not any(character in path for character in (":", "\\", "\uf03a", "\0"))
    )


def main() -> None:
    environment = os.environ.get("UV_PROJECT_ENVIRONMENT", "")
    if not valid_environment(environment):
        sys.exit(
            "Invalid UV_PROJECT_ENVIRONMENT: container uv requires a normalized "
            "absolute /tmp/<name> path. Use /tmp/lumen-dev-venv; "
            "never pass a host Windows path."
        )
    os.execv("/bin/uv-real", ["uv", *sys.argv[1:]])


if __name__ == "__main__":
    main()
