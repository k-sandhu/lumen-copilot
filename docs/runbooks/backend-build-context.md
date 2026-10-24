# Backend build context hygiene (#591)

Git ignore and Docker ignore are separate. A virtualenv's `.gitignore` can hide
it from `git status` while Docker still walks its broken interpreter link.
`backend/.dockerignore` prunes nested `.venv`, `venv`, `*-venv`, ASCII or U+F03A
escaped drive roots, uv/tox/nox caches and `.tmp` directories before traversal.
These exact directory names are reserved for local tooling; source names such as
`venv_tools`, `env_parser.py`, `cache` and `tmp` remain included. Docker ignore
does not filter Compose bind mounts. Arbitrarily named virtualenvs are detected
by preflight via `pyvenv.cfg`; move them outside the checkout before building.

Run this dependency-free read-only preflight from the repository root:

```powershell
python scripts/backend-context.py
python -m unittest discover -s tests -p test_backend_context.py -v
docker compose config --quiet
```

Preflight exits nonzero for virtualenv markers, escaped drive roots, unreadable
paths, symlinks and Windows reparse points. It reports absolute paths using quoted
representations (including escaped Unicode) and never follows links or deletes
files. A drive tree is reported without inspecting its children.

For recovery, inspect each exact reported path in your file manager. Close tools
using it, and move that exact directory or link to a named quarantine directory
outside this checkout. Check the target carefully before moving it. Retain it
until the next build succeeds; no broad recursive deletion is needed. For a
foreign Linux venv, recreate a host venv with host uv instead of reusing it.

The container image sets `UV_PROJECT_ENVIRONMENT=/tmp/lumen-dev-venv`. Its uv
wrapper rejects non-normalized paths and anything outside `/tmp/<name>`, including
Windows drive paths, backslashes and escaped colons, before invoking real uv.
Use the default with `docker compose exec backend uv ...`; do not forward a host
`UV_PROJECT_ENVIRONMENT` into the container. Audit found only two existing
container examples, both already using `/tmp/lumen-dev-venv` (sandbox runbook
and its live-test instructions). Explicitly bypassing the wrapper or overriding
uv's CLI project path is outside this safeguard; use the documented environment.

With a running Docker daemon, exercise Docker's own matcher and cold builds:

```powershell
python scripts/smoke-backend-build.py --images
```

This opt-in smoke copies tracked backend files into a fresh temporary context,
adds eleven 1 MiB fake tooling trees and legitimate similarly named source files,
exports a scratch image filesystem using Docker's real ignore matcher, and
compares every tracked source byte. It prints before/after context file bytes.
Then it cold-builds backend, worker and beat using the real Dockerfile, verifies
uv accepts the POSIX default and refuses a Windows override, and removes only its
unique image tags. Temporary fixture/output directories are removed automatically.
It never starts Compose services or touches existing containers/volumes. Docker's
build cache remains. Omit `--images` for just the matcher/export check. These are
manual build smoke commands; no CI/required-check policy is introduced (OD-7).
