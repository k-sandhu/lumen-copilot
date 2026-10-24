"""Dependency-free regression contracts for issue #591 (run with unittest)."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContextHygiene(unittest.TestCase):
    def test_recursive_exclusions_are_explicit(self):
        patterns = (
            (ROOT / "backend/.dockerignore").read_text(encoding="utf-8").splitlines()
        )
        for pattern in (
            "**/.venv/",
            "**/venv/",
            "**/*-venv/",
            "**/[A-Za-z]:/",
            "**/[A-Za-z]\uf03a/",
            "**/.uv/",
            "**/.tox/",
            "**/.tmp/",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, patterns)

    def test_uv_rejects_host_paths_and_keeps_environment_outside_source(self):
        guard = load("docker_uv", ROOT / "backend/docker-uv.py")
        for path in (
            "C:/Users/dev/env",
            "C:\\Users\\dev",
            "C\uf03a/Users/dev/env",
            "/app/.venv",
            "relative",
            "/tmp/../app/env",
            "/tmp/env\\x",
            "/tmp",
            "",
        ):
            with self.subTest(path=path):
                self.assertFalse(guard.valid_environment(path))
        self.assertTrue(guard.valid_environment("/tmp/lumen-dev-venv"))
        image = (ROOT / "backend/Dockerfile").read_text()
        self.assertIn("UV_PROJECT_ENVIRONMENT=/tmp/lumen-dev-venv", image)
        result = subprocess.run(
            [sys.executable, str(ROOT / "backend/docker-uv.py"), "--version"],
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": "C:/Users/dev/env"},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UV_PROJECT_ENVIRONMENT", result.stderr)

    def test_preflight_reports_exact_paths_without_following_or_deleting(self):
        hygiene = load("context_hygiene", ROOT / "scripts/backend-context.py")
        with tempfile.TemporaryDirectory(prefix="lumen-591-") as temporary:
            root = Path(temporary)
            escaped = root / "nested" / "C\uf03a"
            escaped.mkdir(parents=True)
            (escaped / "unreadable-placeholder").write_text("do not inspect")
            foreign = root / "foreign-env"
            foreign.mkdir()
            (foreign / "pyvenv.cfg").write_text("home = /usr/local/bin\n")
            source = root / "app" / "venv_tools"
            source.mkdir(parents=True)
            (source / "parser.py").write_text("# legitimate source")
            findings = hygiene.inspect(root)
            self.assertEqual(
                {str(escaped.absolute()), str(foreign.absolute())},
                {path for path, reason in findings},
            )
            self.assertTrue((escaped / "unreadable-placeholder").exists())
            self.assertTrue((foreign / "pyvenv.cfg").exists())
            self.assertTrue((source / "parser.py").exists())

    def test_preflight_does_not_follow_directory_links(self):
        hygiene = load("context_hygiene_links", ROOT / "scripts/backend-context.py")
        with tempfile.TemporaryDirectory(prefix="lumen-591-") as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (outside / "pyvenv.cfg").write_text("home = /usr/local/bin")
            backend = root / "backend"
            backend.mkdir()
            link = backend / "external"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlink creation requires Windows privilege")
            self.assertEqual(
                [(str(link.absolute()), "link/reparse point (not followed)")],
                hygiene.inspect(backend),
            )


if __name__ == "__main__":
    unittest.main()
