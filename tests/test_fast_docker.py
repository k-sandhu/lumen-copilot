"""Resolved Compose contract for the opt-in local fast runtime (issue #649)."""

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def resolve(*files: str) -> dict:
    command = ["docker", "compose", "-p", "fast-mode-contract"]
    for filename in files:
        command.extend(["-f", str(ROOT / filename)])
    command.extend(["config", "--format", "json", "--no-env-resolution"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class FastDockerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = resolve("docker-compose.yml")
        cls.fast = resolve("docker-compose.yml", "docker-compose.fast.yml")

    def test_runtime_has_no_application_source_or_dependency_mounts(self) -> None:
        for name in ("backend", "worker", "beat", "frontend"):
            with self.subTest(service=name):
                self.assertEqual(self.fast["services"][name].get("volumes", []), [])

    def test_fast_mode_preserves_datastores_ports_and_trust_networks(self) -> None:
        for name in ("postgres", "redis", "minio", "opensearch"):
            self.assertEqual(self.fast["services"][name], self.base["services"][name])
        for name in ("frontend", "backend"):
            self.assertEqual(
                self.fast["services"][name]["ports"], self.base["services"][name]["ports"]
            )
        for name in ("backend", "worker"):
            self.assertEqual(
                self.fast["services"][name]["networks"],
                self.base["services"][name]["networks"],
            )
        for name in ("pgdata", "redisdata", "miniodata", "osdata"):
            self.assertEqual(self.fast["volumes"][name], self.base["volumes"][name])

    def test_api_has_no_reload_and_worker_has_explicit_small_concurrency(self) -> None:
        api = self.fast["services"]["backend"]["command"]
        self.assertNotIn("--reload", api)
        self.assertEqual(api[api.index("--workers") + 1], "2")
        worker = self.fast["services"]["worker"]["command"]
        self.assertIn("--concurrency=2", worker)
        self.assertEqual(self.fast["services"]["backend"]["stop_grace_period"], "30s")
        self.assertEqual(self.fast["services"]["worker"]["stop_grace_period"], "1m0s")

    def test_static_frontend_build_and_liveness_are_explicit(self) -> None:
        frontend = self.fast["services"]["frontend"]
        self.assertEqual(frontend["build"]["dockerfile"], "frontend/Dockerfile.fast")
        self.assertIn("/_frontend_health", " ".join(frontend["healthcheck"]["test"]))
        self.assertNotIn("dev", frontend.get("command") or [])


if __name__ == "__main__":
    unittest.main()
