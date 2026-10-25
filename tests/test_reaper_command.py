"""Execute the resolved multipart-reaper shell, not its YAML text (#804)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def posix_shell() -> str:
    shell = shutil.which("sh")
    if shell:
        return shell
    git_shell = (
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/sh.exe"
    )
    if git_shell.is_file():
        return str(git_shell)
    raise unittest.SkipTest("requires a POSIX shell (Git for Windows supplies sh.exe)")


class ReaperCommandContract(unittest.TestCase):
    def test_flags_and_bucket_reach_one_mc_invocation_and_interval_is_preserved(
        self,
    ) -> None:
        resolved = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ROOT / ".env.example"),
                "-f",
                str(ROOT / "docker-compose.yml"),
                "config",
                "--format",
                "json",
                "--no-env-resolution",
            ],
            check=True,
            capture_output=True,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "UPLOAD_INCOMPLETE_LIFECYCLE_DAYS",
                    "UPLOAD_MULTIPART_REAPER_INTERVAL_SECONDS",
                }
            },
        )
        service = json.loads(resolved.stdout)["services"]["minio-multipart-reaper"]
        self.assertEqual(service["entrypoint"], ["/bin/sh", "-c"])
        self.assertEqual(
            service["environment"]["UPLOAD_INCOMPLETE_LIFECYCLE_DAYS"], "2"
        )
        self.assertEqual(
            service["environment"]["UPLOAD_MULTIPART_REAPER_INTERVAL_SECONDS"], "900"
        )
        for age, seconds in (("2", "900"), ("0", "7")):
            with tempfile.TemporaryDirectory(prefix="lumen-804-") as temporary:
                directory = Path(temporary)
                stub = directory / "mc"
                stub.write_text(
                    '#!/bin/sh\n{ printf "mc"; printf "\\t%s" "$@"; printf "\\n"; } >> "$REAPER_RECORD"\n',
                    encoding="utf-8",
                )
                stub.chmod(0o755)
                record = directory / "record"
                interval = directory / "interval"
                env = {
                    **os.environ,
                    **service["environment"],
                    "MINIO_ROOT_USER": "test user",
                    "MINIO_ROOT_PASSWORD": "test password",
                    "S3_BUCKET": "unit-bucket",
                    "STUB_DIR": str(directory),
                    "UPLOAD_INCOMPLETE_LIFECYCLE_DAYS": age,
                    "UPLOAD_MULTIPART_REAPER_INTERVAL_SECONDS": seconds,
                    "REAPER_RECORD": str(record),
                    "REAPER_INTERVAL": str(interval),
                }
                # The successful alias stub avoids startup retries. The sleep function
                # records the real interval then exits this shell after ONE iteration.
                command = (
                    'PATH="$(cd "$STUB_DIR" && pwd):$PATH"; '
                    'sleep() { printf "%s" "$1" > "$REAPER_INTERVAL"; exit 0; }; '
                    # Compose config serializes literal dollars escaped for re-loading.
                    # The container engine receives one dollar for each escaped pair.
                    + service["command"][0].replace("$$", "$")
                )
                completed = subprocess.run(
                    [posix_shell(), "-c", command],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                self.assertEqual(completed.stderr, "")
                calls = [line.split("\t") for line in record.read_text().splitlines()]
                self.assertEqual(
                    calls,
                    [
                        [
                            "mc",
                            "alias",
                            "set",
                            "local",
                            "http://minio:9000",
                            "test user",
                            "test password",
                        ],
                        [
                            "mc",
                            "rm",
                            "--incomplete",
                            "--recursive",
                            "--force",
                            "--older-than",
                            f"{age}d",
                            "local/unit-bucket/",
                        ],
                    ],
                )
                self.assertEqual(interval.read_text(), seconds)


if __name__ == "__main__":
    unittest.main()
