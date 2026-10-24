"""Opt-in real Docker matcher + cold image smoke for #591 (no running stack)."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--images",
        action="store_true",
        help="also cold-build backend, worker and beat with unique tags",
    )
    args = parser.parse_args()
    run("docker", "info", stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="lumen-591-smoke-") as temporary:
        root = Path(temporary)
        context = root / "context"
        context.mkdir()
        tracked = run(
            "git", "-C", str(ROOT), "ls-files", "backend", capture_output=True
        ).stdout.splitlines()
        expected = []
        for filename in tracked:
            relative = Path(filename).relative_to("backend")
            target = context / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / filename, target)
            expected.append(relative)
        # Include uncommitted new tooling when validating before the commit.
        for filename in (".dockerignore", "Dockerfile", "docker-uv.py"):
            shutil.copyfile(ROOT / "backend" / filename, context / filename)
            if Path(filename) not in expected:
                expected.append(Path(filename))
        excluded = [
            ".venv",
            "nested/venv",
            "nested/deep/lumen-dev-venv",
            "C\uf03a/Users/dev/lumen-dev-venv",
            "nested/D\uf03a/Users/dev",
            "nested/.uv",
            "nested/.uv-cache",
            "nested/.tox",
            "nested/.nox",
            "nested/.tmp",
            "nested/.cache/uv",
        ]
        for directory in excluded:
            target = context / directory / "bin/python"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * 1024 * 1024)
        included = [
            "app/venv_tools/source.py",
            "app/env_parser.py",
            "app/cache/source.py",
            "app/convert-venv.py",
            "app/tmp/source.py",
        ]
        for filename in included:
            target = context / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# legitimate source\n")
        output = root / "output"
        run(
            "docker",
            "build",
            "--no-cache",
            "--progress=plain",
            "--output",
            f"type=local,dest={output}",
            "-f",
            "-",
            str(context),
            input="FROM scratch\nCOPY . /\n",
        )
        for directory in excluded:
            assert not (output / directory).exists(), f"Docker included {directory}"
        for filename in [*expected, *map(Path, included)]:
            assert (output / filename).read_bytes() == (
                context / filename
            ).read_bytes(), f"source missing/changed: {filename}"
        before = sum(p.stat().st_size for p in context.rglob("*") if p.is_file())
        after = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
        print(
            json.dumps(
                {
                    "fixture_bytes": before,
                    "exported_bytes": after,
                    "excluded_bytes": before - after,
                    "source_files_verified": len(expected) + len(included),
                }
            )
        )
        if args.images:
            project = "lumen-591-" + uuid.uuid4().hex[:12]
            # Only these unique image names are written. No up/down/ports/volumes.
            compose = root / "compose.json"
            compose.write_text(
                json.dumps(
                    {
                        "services": {
                            service: {
                                "image": f"{project}-{service}",
                                "build": {"context": str(context)},
                            }
                            for service in ("backend", "worker", "beat")
                        }
                    }
                )
            )
            tags = [f"{project}-{service}" for service in ("backend", "worker", "beat")]
            try:
                run(
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    str(compose),
                    "build",
                    "--no-cache",
                    "backend",
                    "worker",
                    "beat",
                )
                run(
                    "docker",
                    "run",
                    "--rm",
                    "--network=none",
                    "--entrypoint",
                    "/bin/uv",
                    tags[0],
                    "--version",
                )
                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network=none",
                        "--entrypoint",
                        "/bin/uv",
                        "-e",
                        "UV_PROJECT_ENVIRONMENT=C:/Users/dev/env",
                        tags[0],
                        "--version",
                    ],
                    capture_output=True,
                    text=True,
                )
                assert (
                    result.returncode != 0
                    and "Invalid UV_PROJECT_ENVIRONMENT" in result.stderr
                ), result.stderr
            finally:
                subprocess.run(["docker", "image", "rm", *tags], check=False)


if __name__ == "__main__":
    main()
