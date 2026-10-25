"""Pinned local MinIO supply contract (#795), offline and dependency-free."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MinioImageContract(unittest.TestCase):
    def test_both_services_build_the_same_local_image_without_registry_pull(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for name in ("minio", "minio-multipart-reaper"):
            # Extract with indentation preserved until the next service.
            section = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", compose, re.M | re.S)[1]
            self.assertIn("image: lumen-minio:RELEASE.2024-10-13T13-34-11Z-1", section)
            self.assertIn("context: ./docker/minio", section)
            self.assertIn("pull_policy: build", section)
        self.assertNotIn("image: minio/minio:", compose)

    def test_official_assets_have_fixed_checksum_and_pinned_base(self):
        dockerfile = (ROOT / "docker/minio/Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"FROM alpine:3\.20\.3@sha256:[0-9a-f]{64}")
        assets = re.findall(r"ADD --checksum=sha256:([0-9a-f]{64})\s+(https://\S+)\s+(\S+)", dockerfile)
        self.assertEqual(len(assets), 4)
        for checksum, url, destination in assets:
            self.assertRegex(url, r"^https://github.com/minio/(minio|mc)/releases/download/RELEASE\.")
            self.assertIn(destination, ("/usr/bin/minio", "/usr/bin/mc"))
        self.assertIn("1126bbb3276321e7fed0167682d92a03572862f73802658f616af43d9a951e6a", dockerfile)
        self.assertNotIn("ARG MINIO", dockerfile)
        self.assertIn("FROM binaries-${TARGETARCH}", dockerfile)


if __name__ == "__main__":
    unittest.main()
