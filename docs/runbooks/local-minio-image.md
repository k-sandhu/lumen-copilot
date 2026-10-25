# Local MinIO image supply

Issue #795 preserves MinIO `RELEASE.2024-10-13T13-34-11Z`. Both the server and
multipart reaper build `docker/minio/Dockerfile` locally; `pull_policy: build`
prevents Compose from looking for the local image tag in a registry. Normal
startup remains `docker compose up --build`. Docker with BuildKit is required
(`ADD --checksum`, Docker 25+); Linux amd64 and arm64 are supported. Other
architectures fail the build rather than selecting an incompatible binary.

## Provenance and availability

On 2026-10-25 a clean, unauthenticated Docker engine returned pull-access-denied
for the former Docker Hub image. Docker Hub's public tag API returned 404 and
Quay's manifest endpoint returned 401. This establishes that the configured
registry/tag cannot supply an anonymous cold start; it does not establish why
upstream changed access or whether the change is permanent. Registry login is
not part of the local-run setup.

The replacement downloads the same server release from the
[official MinIO GitHub release](https://github.com/minio/minio/releases/tag/RELEASE.2024-10-13T13-34-11Z)
and the client from the
[official mc release](https://github.com/minio/mc/releases/tag/RELEASE.2024-10-08T09-37-26Z).
Each architecture's SHA256 is recorded in the Dockerfile from the release's
`.sha256sum` asset. The amd64 server hash is
`1126bbb3276321e7fed0167682d92a03572862f73802658f616af43d9a951e6a`;
its reported source commit is `d10bb7e1b667c2df72c394ef1fa52ab4a6802d0f`.
The Alpine base is pinned by version and multi-platform manifest digest.
There is no package installation or moving artifact URL in the build.

BuildKit checks every asset against its fixed checksum before committing a
layer. A download failure or mismatch fails the build. No checksum build argument
can silently bypass this check. Release updates must update URLs and hashes
in review together. This is a reproducible image recipe around official binaries,
not a claim that rebuilding MinIO source produces byte-identical binaries.
MinIO and mc retain their upstream AGPL-3.0 licensing; source is linked above.

## Verification

Offline structural regression:

```powershell
python -m unittest discover -s tests -p test_minio_image.py -v
docker compose --env-file .env.example config --no-env-resolution --quiet
```

Cold build without registry credentials (use a fresh Docker config directory),
then start only MinIO and its reaper with unique project and host ports:

```powershell
docker compose --env-file .env.example build --no-cache --pull minio minio-multipart-reaper
docker compose --env-file .env.example -p lumen-qa-795 up -d minio minio-multipart-reaper
docker compose --env-file .env.example -p lumen-qa-795 ps
```

For an isolated run, override `MINIO_PORT` and `MINIO_CONSOLE_PORT` before starting;
keep them distinct from other projects. Wait for the `/minio/health/ready` health
check, then use the bundled `mc` to create a uniquely named temporary bucket,
upload bytes, read and compare them, delete the object and bucket, and verify the
object is absent. Exercise the companion's `mc rm --incomplete` command too.
For the negative case, build a temporary copy of the Dockerfile with one expected
checksum replaced by 64 zeroes: BuildKit must reject it with a checksum mismatch.
Never modify the committed hash for a successful build.

Finally `docker compose --env-file .env.example -p lumen-qa-795 down -v` removes
only that verification project's containers/network/volume. Do not reset another
running stack. This change does not change bucket CORS bootstrap (#797). The reaper's
shell invocation repair and provider cleanup checks are documented in the
[multipart reaper runbook](multipart-reaper.md) (#804); a running companion alone
does not prove cleanup succeeded.
